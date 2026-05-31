import hashlib
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.strip().lower().startswith("sqlite")


def _build_connect_args(database_url: str, connect_timeout_seconds: int | None) -> dict[str, Any]:
    if _is_sqlite_url(database_url):
        return {"check_same_thread": False}
    if connect_timeout_seconds is None or connect_timeout_seconds <= 0:
        return {}
    return {"connect_timeout": int(connect_timeout_seconds)}


settings = get_settings()

is_sqlite = _is_sqlite_url(settings.database_url)
connect_args = _build_connect_args(settings.database_url, settings.db_connect_timeout_seconds)

_AGENT_RUN_CURRENT_COLUMNS = (
    "run_id",
    "team_id",
    "user_id",
    "conversation_id",
    "space_id",
    "app_id",
    "app_version",
    "trigger_channel",
    "task",
    "status",
    "final_answer",
    "model",
    "dry_run",
    "max_steps",
    "required_confirmations_json",
    "request_payload_json",
    "loop_state_json",
    "model_routes_json",
    "latency_ms",
    "created_at",
    "completed_at",
)
_AGENT_RUN_LEGACY_COLUMNS = {"current_step", "result_summary", "error_message", "updated_at"}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    class_=Session,
    future=True,
)


def get_db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    try:
        from app.models import (  # noqa: F401
            agent_app,
            agent_app_version,
            agent_run,
            agent_step,
            answer_favorite,
            chat_history,
            chunk_embedding,
            conclusion,
            conversation,
            document,
            document_chunk,
            embedding_model_config,
            incident,
            llm_model_config,
            memory_card,
            memory_card_embedding,
            project_space,
            team,
            user,
        )

        if _should_run_legacy_init(settings.database_url, settings.app_env, settings.db_legacy_init_enabled):
            Base.metadata.create_all(bind=engine)
            _ensure_phase1_columns()
            return

        _ensure_schema_is_alembic_head()
    except OperationalError as exc:
        raise RuntimeError(
            "Database connection failed during application startup. "
            "Check DATABASE_URL in .env. For local development, either start PostgreSQL "
            "or switch DATABASE_URL to a SQLite URL such as 'sqlite:///./CaiBao.db'."
        ) from exc


def _should_run_legacy_init(database_url: str, app_env: str, explicit: bool | None) -> bool:
    if explicit is not None:
        enabled = bool(explicit)
        if enabled and not _is_sqlite_url(database_url):
            raise RuntimeError(
                "Legacy DB bootstrap is forbidden for non-SQLite databases. "
                "Use Alembic migrations and set DB_LEGACY_INIT_ENABLED=false."
            )
    else:
        enabled = app_env.strip().lower() != "prod" and _is_sqlite_url(database_url)

    if app_env.strip().lower() == "prod" and enabled:
        raise RuntimeError(
            "Legacy DB bootstrap is forbidden in prod. Set DB_LEGACY_INIT_ENABLED=false and run 'alembic upgrade head'."
        )
    return enabled


def _ensure_schema_is_alembic_head() -> None:
    current_revision = _get_current_alembic_revision()
    head_revision = _get_head_alembic_revision()
    if current_revision != head_revision:
        raise RuntimeError(
            "Database schema is not at Alembic head. "
            f"current={current_revision or '<none>'}, head={head_revision}. "
            "Run 'alembic upgrade head' before starting the application."
        )


def _get_current_alembic_revision() -> str | None:
    with engine.begin() as conn:
        table_names = set(inspect(conn).get_table_names())
        if "alembic_version" not in table_names:
            return None
        result = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one_or_none()
        if result is None:
            return None
        return str(result).strip() or None


def _ensure_agent_steps_columns(step_cols: set) -> None:
    """Migrate agent_steps from old schema to current model."""
    _add_col("agent_steps", "team_id", "VARCHAR(64) NOT NULL DEFAULT ''", step_cols)
    _add_col("agent_steps", "user_id", "VARCHAR(64) NOT NULL DEFAULT ''", step_cols)
    _add_col("agent_steps", "title", "VARCHAR(128) NOT NULL DEFAULT ''", step_cols)
    _add_col("agent_steps", "input_json", "TEXT NOT NULL DEFAULT '{}'", step_cols)
    _add_col("agent_steps", "output_json", "TEXT NOT NULL DEFAULT '{}'", step_cols)
    _add_col("agent_steps", "error_message", "TEXT", step_cols)
    _add_col("agent_steps", "latency_ms", "INTEGER", step_cols)


def _ensure_memory_card_columns(memory_cols: set) -> None:
    _add_col("memory_cards", "source_ref", "VARCHAR(128)", memory_cols)
    _add_col("memory_cards", "memory_type", "VARCHAR(32) NOT NULL DEFAULT 'card'", memory_cols)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_memory_cards_source_ref "
            "ON memory_cards(source_ref)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_memory_cards_team_user_space_ref_type "
            "ON memory_cards(team_id, user_id, space_id, source_ref, memory_type)"
        )


def _add_col(table: str, col: str, typedef: str, existing: set) -> None:
    if col not in existing:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
        existing.add(col)


def _normalize_legacy_agent_runs_table() -> None:
    """Rebuild old SQLite agent_runs tables that still require current_step."""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if "agent_runs" not in set(inspector.get_table_names()):
            return
        columns = {item["name"] for item in inspector.get_columns("agent_runs")}
        if not (_AGENT_RUN_LEGACY_COLUMNS & columns):
            return

        select_list = ", ".join(
            _agent_run_select_expression(column, columns)
            for column in _AGENT_RUN_CURRENT_COLUMNS
        )
        insert_columns = ", ".join(f'"{column}"' for column in _AGENT_RUN_CURRENT_COLUMNS)

        conn.exec_driver_sql("DROP TABLE IF EXISTS agent_runs__normalized")
        conn.exec_driver_sql(
            """
            CREATE TABLE agent_runs__normalized (
                run_id VARCHAR(36) NOT NULL,
                team_id VARCHAR(64) NOT NULL,
                user_id VARCHAR(64) NOT NULL,
                conversation_id VARCHAR(36),
                space_id VARCHAR(36),
                app_id VARCHAR(36),
                app_version INTEGER,
                trigger_channel VARCHAR(32) NOT NULL DEFAULT 'agent',
                task TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'running',
                final_answer TEXT NOT NULL DEFAULT '',
                model VARCHAR(128),
                dry_run BOOLEAN NOT NULL DEFAULT 0,
                max_steps INTEGER NOT NULL DEFAULT 5,
                required_confirmations_json TEXT NOT NULL DEFAULT '[]',
                request_payload_json TEXT NOT NULL DEFAULT '{}',
                loop_state_json TEXT,
                model_routes_json TEXT,
                latency_ms INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                completed_at DATETIME,
                PRIMARY KEY (run_id),
                FOREIGN KEY(conversation_id) REFERENCES conversations (conversation_id),
                FOREIGN KEY(space_id) REFERENCES project_spaces (space_id),
                FOREIGN KEY(team_id) REFERENCES teams (team_id),
                FOREIGN KEY(user_id) REFERENCES users (user_id),
                FOREIGN KEY(app_id) REFERENCES agent_apps (app_id)
            )
            """
        )
        conn.exec_driver_sql(
            f"""
            INSERT INTO agent_runs__normalized ({insert_columns})
            SELECT {select_list}
            FROM agent_runs
            """
        )
        conn.exec_driver_sql("DROP TABLE agent_runs")
        conn.exec_driver_sql("ALTER TABLE agent_runs__normalized RENAME TO agent_runs")
        _create_agent_run_indexes(conn)


def _agent_run_select_expression(column: str, columns: set[str]) -> str:
    if column in columns:
        source = f'"{column}"'
    else:
        source = _agent_run_missing_value(column)

    default = _agent_run_coalesce_default(column)
    if default is not None:
        source = f"COALESCE({source}, {default})"
    return f'{source} AS "{column}"'


def _agent_run_missing_value(column: str) -> str:
    return {
        "trigger_channel": "'agent'",
        "task": "''",
        "status": "'running'",
        "final_answer": "''",
        "dry_run": "0",
        "max_steps": "5",
        "required_confirmations_json": "'[]'",
        "request_payload_json": "'{}'",
        "created_at": "CURRENT_TIMESTAMP",
    }.get(column, "NULL")


def _agent_run_coalesce_default(column: str) -> str | None:
    return {
        "trigger_channel": "'agent'",
        "task": "''",
        "status": "'running'",
        "final_answer": "''",
        "dry_run": "0",
        "max_steps": "5",
        "required_confirmations_json": "'[]'",
        "request_payload_json": "'{}'",
        "created_at": "CURRENT_TIMESTAMP",
    }.get(column)


def _create_agent_run_indexes(conn: Any) -> None:
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_runs_team_id ON agent_runs(team_id)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_runs_user_id ON agent_runs(user_id)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_runs_conversation_id ON agent_runs(conversation_id)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_runs_space_id ON agent_runs(space_id)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_runs_status ON agent_runs(status)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_runs_dry_run ON agent_runs(dry_run)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_runs_app_id ON agent_runs(app_id)")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_runs_team_user_created_at "
        "ON agent_runs(team_id, user_id, created_at)"
    )


def _get_head_alembic_revision() -> str:
    alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    if not alembic_ini.exists():
        raise RuntimeError(f"alembic.ini not found at '{alembic_ini}'.")
    config = Config(str(alembic_ini))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()


def _ensure_phase1_columns() -> None:
    """Best-effort lightweight migration for existing DB files."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    if "users" in table_names:
        user_cols = {item["name"] for item in inspector.get_columns("users")}
        if "password_hash" not in user_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)")
        if "is_active" not in user_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1")
        if "password_updated_at" not in user_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN password_updated_at DATETIME")

    if "conversations" in table_names:
        conversation_cols = {item["name"] for item in inspector.get_columns("conversations")}
        if "is_pinned" not in conversation_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE conversations ADD COLUMN is_pinned BOOLEAN DEFAULT 0")
        if "pinned_at" not in conversation_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE conversations ADD COLUMN pinned_at DATETIME")
        if "space_id" not in conversation_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE conversations ADD COLUMN space_id VARCHAR(36)")

    if "documents" in table_names:
        document_cols = {item["name"] for item in inspector.get_columns("documents")}
        if "conversation_id" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN conversation_id VARCHAR(36)")
        if "status" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN status VARCHAR(16) DEFAULT 'pending'")

    if "agent_runs" in table_names:
        agent_run_cols = {item["name"] for item in inspector.get_columns("agent_runs")}
        if "app_id" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN app_id VARCHAR(36)")
        if "app_version" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN app_version INTEGER")
        if "trigger_channel" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN trigger_channel VARCHAR(32) DEFAULT 'agent'")
        # v0.23.0+: 旧表 goal → task 重命名 + 缺失列补充
        if "goal" in agent_run_cols and "task" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs RENAME COLUMN goal TO task")
            agent_run_cols.discard("goal")
            agent_run_cols.add("task")
        if "task" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN task TEXT NOT NULL DEFAULT ''")
        if "final_answer" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN final_answer TEXT NOT NULL DEFAULT ''")
        if "model" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN model VARCHAR(128)")
        if "dry_run" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN dry_run BOOLEAN NOT NULL DEFAULT 0")
        if "required_confirmations_json" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN required_confirmations_json TEXT NOT NULL DEFAULT '[]'")
        if "request_payload_json" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN request_payload_json TEXT NOT NULL DEFAULT '{}'")
        if "loop_state_json" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN loop_state_json TEXT")
        if "model_routes_json" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN model_routes_json TEXT")
        if "latency_ms" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN latency_ms INTEGER")
        if "completed_at" not in agent_run_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN completed_at DATETIME")
        _normalize_legacy_agent_runs_table()

    # agent_steps 旧→新 schema 迁移
    if "agent_steps" in table_names:
        step_cols = {item["name"] for item in inspector.get_columns("agent_steps")}
        _ensure_agent_steps_columns(step_cols)

    if "memory_cards" in table_names:
        memory_cols = {item["name"] for item in inspector.get_columns("memory_cards")}
        _ensure_memory_card_columns(memory_cols)

    if "documents" in table_names:
        document_cols = {item["name"] for item in inspector.get_columns("documents")}
        if "mime_type" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN mime_type VARCHAR(128)")
        if "size_bytes" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN size_bytes BIGINT")
        if "sha256" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN sha256 VARCHAR(64)")
        if "storage_key" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN storage_key VARCHAR(512)")
        if "preview_key" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN preview_key VARCHAR(512)")
        if "page_count" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN page_count INTEGER")
        if "failure_stage" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN failure_stage VARCHAR(16)")
        if "error_code" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN error_code VARCHAR(64)")
        if "error_message" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN error_message TEXT")
        if "meta_json" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN meta_json TEXT")
        if "updated_at" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN updated_at DATETIME")
        if "space_id" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN space_id VARCHAR(36)")
        if "visibility" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN visibility VARCHAR(16) DEFAULT 'conversation'")
        if "asset_kind" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN asset_kind VARCHAR(32) DEFAULT 'attachment'")
        if "retrieval_enabled" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN retrieval_enabled BOOLEAN DEFAULT 1")
        if "origin_document_id" not in document_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN origin_document_id VARCHAR(36)")

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_documents_team_conversation_created_at "
                "ON documents(team_id, conversation_id, created_at)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_documents_team_status_created_at "
                "ON documents(team_id, status, created_at)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_documents_team_sha256 "
                "ON documents(team_id, sha256)"
            )

            rows = conn.exec_driver_sql(
                """
                SELECT document_id, content_type, status, content, created_at, updated_at,
                       mime_type, size_bytes, sha256, storage_key
                FROM documents
                """
            ).mappings().all()

            now_iso = datetime.now(timezone.utc).isoformat()
            mime_by_type = {
                "txt": "text/plain",
                "md": "text/markdown",
                "pdf": "application/pdf",
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
            }
            allowed_statuses = {
                "pending",
                "uploaded",
                "parsing",
                "chunking",
                "indexing",
                "ready",
                "failed",
                "deleted",
            }
            for row in rows:
                content_type = str(row.get("content_type") or "md").strip().lower() or "md"
                if content_type not in mime_by_type:
                    content_type = "md"

                content = row.get("content")
                content_text = str(content) if isinstance(content, str) else ""
                if content_text:
                    digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
                else:
                    digest = hashlib.sha256(str(row["document_id"]).encode("utf-8")).hexdigest()

                status = str(row.get("status") or "").strip().lower() or "pending"
                if status not in allowed_statuses:
                    status = "pending"

                created_at = row.get("created_at")
                updated_at = row.get("updated_at") or created_at or now_iso
                storage_key = str(row.get("storage_key") or "").strip() or f"inline://legacy/{row['document_id']}"

                conn.exec_driver_sql(
                    """
                    UPDATE documents
                    SET content_type = :content_type,
                        status = :status,
                        mime_type = :mime_type,
                        size_bytes = :size_bytes,
                        sha256 = :sha256,
                        storage_key = :storage_key,
                        updated_at = :updated_at
                    WHERE document_id = :document_id
                    """,
                    {
                        "document_id": row["document_id"],
                        "content_type": content_type,
                        "status": status,
                        "mime_type": str(row.get("mime_type") or "").strip() or mime_by_type[content_type],
                        "size_bytes": int(row.get("size_bytes") or len(content_text.encode("utf-8"))),
                        "sha256": str(row.get("sha256") or "").strip() or digest,
                        "storage_key": storage_key,
                        "updated_at": updated_at,
                    },
                )

    if "document_chunks" in table_names:
        chunk_cols = {item["name"] for item in inspector.get_columns("document_chunks")}
        if "page_no" not in chunk_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE document_chunks ADD COLUMN page_no INTEGER")
        if "locator_label" not in chunk_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE document_chunks ADD COLUMN locator_label VARCHAR(64)")
        if "block_type" not in chunk_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE document_chunks ADD COLUMN block_type VARCHAR(16)")
        if "meta_json" not in chunk_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE document_chunks ADD COLUMN meta_json TEXT")
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_document_page_chunk "
                "ON document_chunks(document_id, page_no, chunk_index)"
            )

    if "chat_history" in table_names:
        history_cols = {item["name"] for item in inspector.get_columns("chat_history")}
        if "conversation_id" not in history_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE chat_history ADD COLUMN conversation_id VARCHAR(36)")
        if "space_id" not in history_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE chat_history ADD COLUMN space_id VARCHAR(36)")

    if "answer_favorites" in table_names:
        favorite_cols = {item["name"] for item in inspector.get_columns("answer_favorites")}
        if "updated_at" not in favorite_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE answer_favorites ADD COLUMN updated_at DATETIME")
        if "is_promoted" not in favorite_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE answer_favorites ADD COLUMN is_promoted BOOLEAN DEFAULT 0")

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_answer_favorites_team_space_user_created_at "
                "ON answer_favorites(team_id, space_id, user_id, created_at)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_answer_favorites_team_conversation_created_at "
                "ON answer_favorites(team_id, conversation_id, created_at)"
            )

            now_iso = datetime.now(timezone.utc).isoformat()
            conn.exec_driver_sql(
                """
                UPDATE answer_favorites
                SET updated_at = COALESCE(updated_at, created_at, :now_iso)
                """,
                {"now_iso": now_iso},
            )

    if "conclusions" in table_names:
        conclusion_cols = {item["name"] for item in inspector.get_columns("conclusions")}
        if "updated_at" not in conclusion_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE conclusions ADD COLUMN updated_at DATETIME")

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_conclusions_team_space_status_updated_at "
                "ON conclusions(team_id, space_id, status, updated_at)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_conclusions_team_topic_created_at "
                "ON conclusions(team_id, topic, created_at)"
            )

            now_iso = datetime.now(timezone.utc).isoformat()
            conn.exec_driver_sql(
                """
                UPDATE conclusions
                SET updated_at = COALESCE(updated_at, created_at, :now_iso)
                """,
                {"now_iso": now_iso},
            )
