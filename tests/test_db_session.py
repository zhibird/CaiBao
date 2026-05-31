import app.db.session as session_module
import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from app.db.session import _is_sqlite_url
from app.db.session import _build_connect_args
from app.db.session import _should_run_legacy_init


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        ("sqlite:///./local.db", True),
        ("sqlite+pysqlite:///./local.db", True),
        ("postgresql+psycopg://user:pass@localhost:5432/caibao", False),
        ("postgres://user:pass@localhost:5432/caibao", False),
        ("mysql+pymysql://user:pass@localhost:3306/caibao", False),
    ],
)
def test_is_sqlite_url_recognizes_only_sqlite_urls(database_url: str, expected: bool):
    assert _is_sqlite_url(database_url) is expected


def test_build_connect_args_uses_sqlite_thread_flag():
    assert _build_connect_args("sqlite:///./local.db", 5) == {"check_same_thread": False}


def test_build_connect_args_uses_connect_timeout_for_postgresql():
    assert _build_connect_args("postgresql+psycopg://user:pass@localhost:5432/caibao", 5) == {
        "connect_timeout": 5
    }


def test_legacy_init_cannot_be_enabled_for_postgresql():
    with pytest.raises(RuntimeError, match="Legacy DB bootstrap is forbidden for non-SQLite databases"):
        _should_run_legacy_init(
            database_url="postgresql+psycopg://user:pass@localhost:5432/caibao",
            app_env="dev",
            explicit=True,
        )


def test_legacy_init_defaults_to_false_in_prod():
    assert (
        _should_run_legacy_init(
            database_url="sqlite:///./local.db",
            app_env="prod",
            explicit=None,
        )
        is False
    )


def test_legacy_init_defaults_to_false_for_postgresql_in_dev():
    assert (
        _should_run_legacy_init(
            database_url="postgresql+psycopg://user:pass@localhost:5432/caibao",
            app_env="dev",
            explicit=None,
        )
        is False
    )


def test_init_db_wraps_database_connection_errors(monkeypatch):
    monkeypatch.setattr(session_module, "_should_run_legacy_init", lambda *_: False)

    def _raise_operational_error():
        raise OperationalError("SELECT 1", {}, Exception("boom"))

    monkeypatch.setattr(session_module, "_ensure_schema_is_alembic_head", _raise_operational_error)

    with pytest.raises(RuntimeError, match="Database connection failed during application startup"):
        session_module.init_db()


def test_ensure_phase1_columns_adds_auth_columns_to_legacy_users_table(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy-auth.db"
    database_url = f"sqlite:///{db_file.resolve().as_posix()}"
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE users (
                    user_id VARCHAR(64) PRIMARY KEY,
                    team_id VARCHAR(64) NOT NULL,
                    display_name VARCHAR(128) NOT NULL,
                    role VARCHAR(32) NOT NULL,
                    created_at DATETIME
                )
                """
            )

        monkeypatch.setattr(session_module, "engine", engine)
        session_module._ensure_phase1_columns()

        with engine.connect() as connection:
            users_columns = {col["name"] for col in inspect(connection).get_columns("users")}
    finally:
        engine.dispose()

    assert {"password_hash", "is_active", "password_updated_at"} <= users_columns


def test_ensure_phase1_columns_normalizes_legacy_agent_runs_table(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy-agent-runs.db"
    database_url = f"sqlite:///{db_file.resolve().as_posix()}"
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE agent_runs (
                    run_id VARCHAR(36) NOT NULL PRIMARY KEY,
                    team_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    conversation_id VARCHAR(36),
                    space_id VARCHAR(36),
                    task TEXT NOT NULL,
                    status VARCHAR(24) NOT NULL,
                    max_steps INTEGER NOT NULL,
                    current_step INTEGER NOT NULL,
                    result_summary TEXT,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO agent_runs (
                    run_id, team_id, user_id, task, status, max_steps, current_step
                )
                VALUES ('run-old', 'team-a', 'user-a', 'hello', 'queued', 5, 0)
                """
            )

        monkeypatch.setattr(session_module, "engine", engine)
        session_module._ensure_phase1_columns()

        with engine.begin() as connection:
            columns = {col["name"] for col in inspect(connection).get_columns("agent_runs")}
            connection.exec_driver_sql(
                """
                INSERT INTO agent_runs (run_id, team_id, user_id, task)
                VALUES ('run-new', 'team-a', 'user-a', 'new task')
                """
            )
            row_count = connection.exec_driver_sql("SELECT COUNT(*) FROM agent_runs").scalar_one()
    finally:
        engine.dispose()

    assert not {"current_step", "result_summary", "error_message", "updated_at"} & columns
    assert {"final_answer", "dry_run", "request_payload_json", "completed_at"} <= columns
    assert row_count == 2


def test_ensure_phase1_columns_adds_memory_source_ref_columns(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy-memory-cards.db"
    database_url = f"sqlite:///{db_file.resolve().as_posix()}"
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE memory_cards (
                    memory_id VARCHAR(36) NOT NULL PRIMARY KEY,
                    team_id VARCHAR(64) NOT NULL,
                    space_id VARCHAR(36),
                    user_id VARCHAR(64) NOT NULL,
                    scope_level VARCHAR(16) NOT NULL DEFAULT 'space',
                    category VARCHAR(32) NOT NULL DEFAULT 'fact',
                    title VARCHAR(128) NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT,
                    weight FLOAT NOT NULL DEFAULT 0.8,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    confidence FLOAT NOT NULL DEFAULT 0.8,
                    source_message_id VARCHAR(36),
                    source_document_id VARCHAR(36),
                    expires_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO memory_cards (
                    memory_id, team_id, user_id, title, content
                )
                VALUES ('memory-old', 'team-a', 'user-a', 'Old', 'legacy card')
                """
            )

        monkeypatch.setattr(session_module, "engine", engine)
        session_module._ensure_phase1_columns()

        with engine.begin() as connection:
            inspector = inspect(connection)
            columns = {col["name"] for col in inspector.get_columns("memory_cards")}
            indexes = {idx["name"] for idx in inspector.get_indexes("memory_cards")}
            memory_type = connection.exec_driver_sql(
                "SELECT memory_type FROM memory_cards WHERE memory_id = 'memory-old'"
            ).scalar_one()
    finally:
        engine.dispose()

    assert {"source_ref", "memory_type"} <= columns
    assert memory_type == "card"
    assert {
        "ix_memory_cards_source_ref",
        "ix_memory_cards_team_user_space_ref_type",
    } <= indexes
