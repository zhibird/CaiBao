import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_UPLOAD_ROOT_DIR, Settings


def test_settings_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_upload_root_dir_defaults_with_postgres_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/caibao")
    monkeypatch.delenv("UPLOAD_ROOT_DIR", raising=False)

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://user:pass@localhost:5432/caibao"
    assert settings.upload_root_dir == DEFAULT_UPLOAD_ROOT_DIR


def test_settings_expose_auth_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///auth-config.db")
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-auth-secret")

    settings = Settings(_env_file=None)

    assert settings.auth_jwt_secret == "test-auth-secret"
    assert settings.auth_jwt_algorithm == "HS256"
    assert settings.auth_access_token_ttl_minutes == 15
    assert settings.auth_refresh_token_ttl_days == 14
    assert settings.auth_access_cookie_name == "caibao_access_token"
    assert settings.auth_refresh_cookie_name == "caibao_refresh_token"
    assert settings.auth_cookie_samesite == "lax"
    assert settings.auth_cookie_secure is False


def test_blank_auth_cookie_domain_normalizes_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///auth-config.db")
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-auth-secret")
    monkeypatch.setenv("AUTH_COOKIE_DOMAIN", "")

    settings = Settings(_env_file=None)

    assert settings.auth_cookie_domain is None


def test_prod_rejects_default_jwt_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///prod-config.db")
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("DEV_ADMIN_ENABLED", "false")

    with pytest.raises(ValidationError, match="AUTH_JWT_SECRET"):
        Settings(_env_file=None)


def test_prod_requires_secure_cookies(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///prod-config.db")
    monkeypatch.setenv("AUTH_JWT_SECRET", "prod-secret-value-with-at-least-32-chars")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("DEV_ADMIN_ENABLED", "false")

    with pytest.raises(ValidationError, match="AUTH_COOKIE_SECURE"):
        Settings(_env_file=None)


def test_prod_rejects_default_dev_admin_token_when_enabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///prod-config.db")
    monkeypatch.setenv("AUTH_JWT_SECRET", "prod-secret-value-with-at-least-32-chars")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("DEV_ADMIN_ENABLED", "true")
    monkeypatch.setenv("DEV_ADMIN_TOKEN", "dev-admin-token")

    with pytest.raises(ValidationError, match="DEV_ADMIN_TOKEN"):
        Settings(_env_file=None)
