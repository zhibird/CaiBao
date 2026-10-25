from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.schema import CreateColumn

from app.db.base import Base
from app.models import *  # noqa: F401,F403

HEAD_REVISION = "20260603_00"
EXPECTED_TABLES = {
    "teams",
    "users",
    "project_spaces",
    "memory_cards",
    "answer_favorites",
    "conclusions",
    "auth_refresh_sessions",
    "agent_runs",
    "agent_run_events",
    "agent_run_workers",
    "agent_steps",
    "agent_apps",
    "agent_app_versions",
    # Phase 3-4
    "proactive_sources",
    "proactive_deliveries",
    "proactive_event_logs",
    "subagent_jobs",
    "peer_agent_configs",
}


def _sqlite_database_url(db_file: Path) -> str:
    return f"sqlite:///{db_file.resolve().as_posix()}"


def _make_alembic_config(database_url: str) -> Config:
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("alembic").resolve()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _create_engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _current_revision(database_url: str) -> str | None:
    engine = _create_engine(database_url)
    try:
        with engine.connect() as connection:
            if "alembic_version" not in inspect(connection).get_table_names():
                return None
            revision = connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one_or_none()
            if revision is None:
                return None
            return str(revision).strip() or None
    finally:
        engine.dispose()


def _table_names(database_url: str) -> set[str]:
    engine = _create_engine(database_url)
    try:
        with engine.connect() as connection:
            return set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def _postgres_database_url() -> str | None:
    configured_url = os.environ.get("TEST_DATABASE_URL")
    if not configured_url:
        return None

    backend_name = make_url(configured_url).get_backend_name()
    if backend_name != "postgresql":
        return None
    return configured_url


def _postgres_schema_database_url(database_url: str, schema_name: str) -> str:
    url = make_url(database_url)
    query = dict(url.query)
    existing_options = str(query.get("options", "")).strip()
    schema_option = f"-csearch_path={schema_name}"
    query["options"] = f"{existing_options} {schema_option}".strip() if existing_options else schema_option
    return url.set(query=query).render_as_string(hide_password=False)


def test_migration_fresh_database_upgrade_head(tmp_path) -> None:
    db_file = tmp_path / "fresh.db"
    database_url = _sqlite_database_url(db_file)
    config = _make_alembic_config(database_url)

    command.upgrade(config, "head")

    assert _current_revision(database_url) == HEAD_REVISION
    assert EXPECTED_TABLES <= _table_names(database_url)


def test_migration_legacy_database_stamp_then_upgrade(tmp_path) -> None:
    db_file = tmp_path / "legacy.db"
    database_url = _sqlite_database_url(db_file)
    engine = _create_engine(database_url)
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    config = _make_alembic_config(database_url)
    command.stamp(config, HEAD_REVISION)
    command.upgrade(config, "head")

    assert _current_revision(database_url) == HEAD_REVISION


def test_migration_upgrade_from_half_upgraded_database(tmp_path) -> None:
    db_file = tmp_path / "half.db"
    database_url = _sqlite_database_url(db_file)
    config = _make_alembic_config(database_url)

    command.upgrade(config, "20260330_00")
    assert _current_revision(database_url) == "20260330_00"

    command.upgrade(config, "head")
    assert _current_revision(database_url) == HEAD_REVISION


def test_migration_removes_legacy_agent_run_required_columns(tmp_path) -> None:
    db_file = tmp_path / "legacy-agent-runs.db"
    database_url = _sqlite_database_url(db_file)
    config = _make_alembic_config(database_url)

    command.upgrade(config, "20260412_00")
    engine = _create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE agent_runs (
                        run_id VARCHAR(36) NOT NULL PRIMARY KEY,
                        team_id VARCHAR(64) NOT NULL,
                        user_id VARCHAR(64) NOT NULL,
                        conversation_id VARCHAR(36),
                        space_id VARCHAR(36),
                        task TEXT NOT NULL,
                        max_steps INTEGER NOT NULL DEFAULT 5,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'running',
                        current_step INTEGER NOT NULL,
                        result_summary TEXT,
                        error_message TEXT,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = _create_engine(database_url)
    try:
        with engine.begin() as connection:
            columns = {col["name"] for col in inspect(connection).get_columns("agent_runs")}
            connection.execute(
                text(
                    """
                    INSERT INTO agent_runs (run_id, team_id, user_id, task)
                    VALUES ('run-new', 'team-a', 'user-a', 'new task')
                    """
                )
            )
    finally:
        engine.dispose()

    assert not {"current_step", "result_summary", "error_message", "updated_at"} & columns
    assert {
        "app_id",
        "app_version",
        "trigger_channel",
        "final_answer",
        "dry_run",
        "required_confirmations_json",
        "request_payload_json",
        "loop_state_json",
        "model_routes_json",
        "execution_identity_json",
        "context_snapshot_json",
        "execution_receipt_json",
        "task_plan_json",
        "latency_ms",
        "completed_at",
    } <= columns


def test_execution_identity_migration_preserves_existing_runs_and_downgrades(tmp_path) -> None:
    database_url = _sqlite_database_url(tmp_path / "execution-identity.db")
    config = _make_alembic_config(database_url)
    command.upgrade(config, "20260531_00")
    engine = _create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_runs (run_id, team_id, user_id, task, request_payload_json) "
                "VALUES ('existing', 'team', 'user', 'preserved task', '{}')"
            ))
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT task, execution_identity_json, context_snapshot_json FROM agent_runs WHERE run_id = 'existing'"
            )).one()
            assert row == ("preserved task", None, None)
            nullable = {column["name"]: column["nullable"] for column in inspect(connection).get_columns("agent_runs")}
            assert nullable["execution_identity_json"] and nullable["context_snapshot_json"]
        command.downgrade(config, "20260531_00")
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("agent_runs")}
            assert not {"execution_identity_json", "context_snapshot_json"} & columns
            assert connection.execute(text("SELECT task FROM agent_runs WHERE run_id = 'existing'")).scalar_one() == "preserved task"
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert _current_revision(database_url) == HEAD_REVISION
    finally:
        engine.dispose()


def test_execution_identity_bootstrap_and_alembic_upgrade_can_share_columns(tmp_path, monkeypatch) -> None:
    import app.db.session as session_module

    database_url = _sqlite_database_url(tmp_path / "bootstrapped-identity.db")
    config = _make_alembic_config(database_url)
    command.upgrade(config, "20260531_00")
    engine = _create_engine(database_url)
    try:
        monkeypatch.setattr(session_module, "engine", engine)
        session_module._ensure_phase1_columns()
        session_module._ensure_phase1_columns()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("agent_runs")}
        assert {"execution_identity_json", "context_snapshot_json"} <= columns
        assert _current_revision(database_url) == HEAD_REVISION
    finally:
        engine.dispose()


def test_execution_receipt_migration_preserves_runs_and_downgrades_cleanly(tmp_path) -> None:
    database_url = _sqlite_database_url(tmp_path / "execution-receipt.db")
    config = _make_alembic_config(database_url)
    command.upgrade(config, "20260601_00")
    engine = _create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_runs (run_id, team_id, user_id, task, request_payload_json) "
                "VALUES ('existing', 'team', 'user', 'preserved task', '{}')"
            ))
        command.upgrade(config, "head")
        with engine.begin() as connection:
            row = connection.execute(text(
                "SELECT task, execution_receipt_json FROM agent_runs WHERE run_id = 'existing'"
            )).one()
            assert row == ("preserved task", None)
            columns = {column["name"]: column for column in inspect(connection).get_columns("agent_runs")}
            assert columns["execution_receipt_json"]["nullable"] is True
            connection.execute(text(
                "UPDATE agent_runs SET execution_receipt_json = :receipt WHERE run_id = 'existing'"
            ), {"receipt": '{"version":1,"source":"api_tool_loop"}'})
        command.downgrade(config, "20260601_00")
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("agent_runs")}
            assert "execution_receipt_json" not in columns
            assert {"execution_identity_json", "context_snapshot_json"} <= columns
            assert connection.execute(text("SELECT task FROM agent_runs WHERE run_id = 'existing'")).scalar_one() == "preserved task"
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert _current_revision(database_url) == HEAD_REVISION
    finally:
        engine.dispose()


def test_execution_receipt_bootstrap_then_alembic_is_idempotent(tmp_path, monkeypatch) -> None:
    import app.db.session as session_module

    database_url = _sqlite_database_url(tmp_path / "bootstrap-receipt.db")
    config = _make_alembic_config(database_url)
    command.upgrade(config, "20260601_00")
    engine = _create_engine(database_url)
    try:
        monkeypatch.setattr(session_module, "engine", engine)
        session_module._ensure_phase1_columns()
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_runs (run_id, team_id, user_id, task, execution_receipt_json) "
                "VALUES ('bootstrap', 'team', 'user', 'preserve receipt', :receipt)"
            ), {"receipt": '{"version":1,"source":"api_tool_loop"}'})
        session_module._ensure_phase1_columns()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT execution_receipt_json FROM agent_runs WHERE run_id = 'bootstrap'"
            )).scalar_one() == '{"version":1,"source":"api_tool_loop"}'
        assert _current_revision(database_url) == HEAD_REVISION
    finally:
        engine.dispose()


def test_task_plan_migration_preserves_previous_facts_and_round_trips(tmp_path) -> None:
    database_url = _sqlite_database_url(tmp_path / "task-plan.db")
    config = _make_alembic_config(database_url)
    command.upgrade(config, "20260602_00")
    engine = _create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_runs (run_id, team_id, user_id, task, execution_receipt_json) "
                "VALUES ('existing', 'team', 'user', 'preserved task', :receipt)"
            ), {"receipt": '{"existing":"receipt must remain"}'})
        command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.execute(text(
                "SELECT task, execution_receipt_json, task_plan_json FROM agent_runs WHERE run_id = 'existing'"
            )).one() == ("preserved task", '{"existing":"receipt must remain"}', None)
            columns = {column["name"]: column for column in inspect(connection).get_columns("agent_runs")}
            assert columns["task_plan_json"]["nullable"] is True
        command.downgrade(config, "20260602_00")
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("agent_runs")}
            assert "task_plan_json" not in columns and "execution_receipt_json" in columns
            assert connection.execute(text(
                "SELECT execution_receipt_json FROM agent_runs WHERE run_id = 'existing'"
            )).scalar_one() == '{"existing":"receipt must remain"}'
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert _current_revision(database_url) == HEAD_REVISION
    finally:
        engine.dispose()


def test_task_plan_bootstrap_and_alembic_share_existing_snapshot_without_reset(tmp_path, monkeypatch) -> None:
    import app.db.session as session_module

    database_url = _sqlite_database_url(tmp_path / "bootstrap-task-plan.db")
    config = _make_alembic_config(database_url)
    command.upgrade(config, "20260602_00")
    engine = _create_engine(database_url)
    try:
        monkeypatch.setattr(session_module, "engine", engine)
        session_module._ensure_phase1_columns()
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_runs (run_id, team_id, user_id, task, task_plan_json) "
                "VALUES ('bootstrap', 'team', 'user', 'task', :snapshot)"
            ), {"snapshot": '{"already":"persisted snapshot"}'})
        session_module._ensure_phase1_columns()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT task_plan_json FROM agent_runs WHERE run_id = 'bootstrap'"
            )).scalar_one() == '{"already":"persisted snapshot"}'
        assert _current_revision(database_url) == HEAD_REVISION
    finally:
        engine.dispose()


def test_auth_migration_adds_auth_tables_and_user_columns(tmp_path) -> None:
    db_file = tmp_path / "auth.db"
    database_url = _sqlite_database_url(db_file)
    config = _make_alembic_config(database_url)

    command.upgrade(config, "head")

    engine = _create_engine(database_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            users_columns = {col["name"] for col in inspector.get_columns("users")}
            refresh_columns = {col["name"] for col in inspector.get_columns("auth_refresh_sessions")}
    finally:
        engine.dispose()

    assert {"password_hash", "is_active", "password_updated_at"} <= users_columns
    assert {"session_id", "user_id", "refresh_token_hash", "expires_at", "revoked_at"} <= refresh_columns


def test_auth_boolean_default_compiles_for_postgresql() -> None:
    ddl = str(
        CreateColumn(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
        ).compile(dialect=postgresql.dialect())
    ).lower()

    assert "boolean" in ddl
    assert "default true" in ddl
    assert "default 1" not in ddl


@pytest.mark.skipif(
    _postgres_database_url() is None,
    reason="TEST_DATABASE_URL is not configured with a PostgreSQL URL",
)
def test_migration_postgresql_upgrade_head_smoke() -> None:
    database_url = _postgres_database_url()
    assert database_url is not None

    admin_engine = _create_engine(database_url)
    schema_name = f"pytest_migration_{uuid4().hex}"
    isolated_url = _postgres_schema_database_url(database_url, schema_name)
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

        assert _current_revision(isolated_url) is None

        config = _make_alembic_config(isolated_url)
        command.upgrade(config, "head")

        assert _current_revision(isolated_url) == HEAD_REVISION
        assert EXPECTED_TABLES <= _table_names(isolated_url)
    finally:
        admin_engine.dispose()
        cleanup_engine = _create_engine(database_url)
        try:
            with cleanup_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            cleanup_engine.dispose()
