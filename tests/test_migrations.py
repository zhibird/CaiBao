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

HEAD_REVISION = "20260528_00"
EXPECTED_TABLES = {
    "teams",
    "users",
    "project_spaces",
    "memory_cards",
    "answer_favorites",
    "conclusions",
    "auth_refresh_sessions",
    "agent_runs",
    "agent_steps",
    "agent_apps",
    "agent_app_versions",
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
