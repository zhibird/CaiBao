import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_UPLOAD_ROOT_DIR, Settings


def _clear_model_env(monkeypatch):
    for key in (
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_FAST_MODEL",
        "LLM_FAST_BASE_URL",
        "LLM_FAST_API_KEY",
        "LLM_VL_MODEL",
        "LLM_VL_BASE_URL",
        "LLM_VL_API_KEY",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


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


def test_config_toml_populates_model_profiles(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///config-toml.db")
    monkeypatch.setenv("DEEPSEEK_KEY", "sk-deepseek")
    monkeypatch.setenv("DASHSCOPE_KEY", "sk-dashscope")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
provider = "deepseek"

[llm.main]
model = "deepseek-v4-flash"
api_key = "${DEEPSEEK_KEY}"
base_url = "https://api.deepseek.com/v1"
enable_thinking = true
multimodal = false

[llm.fast]
model = "qwen-flash"
api_key = "${DASHSCOPE_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

[llm.vl]
model = "qwen-vl-plus"
api_key = "${DASHSCOPE_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

[agent]
system_prompt = "You are CaiBao."
max_tokens = 8192
max_iterations = 40
dev_mode = true

[agent.context]
memory_window = 40

[memory.embedding]
model = "text-embedding-v3"
api_key = "${DASHSCOPE_KEY}"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_TOML_PATH", str(config_path))

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "deepseek"
    assert settings.llm_model == "deepseek-v4-flash"
    assert settings.llm_api_key == "sk-deepseek"
    assert settings.llm_enable_thinking is True
    assert settings.llm_multimodal is False
    assert settings.llm_fast_model == "qwen-flash"
    assert settings.llm_fast_api_key == "sk-dashscope"
    assert settings.llm_vl_model == "qwen-vl-plus"
    assert settings.agent_system_prompt == "You are CaiBao."
    assert settings.llm_max_tokens == 8192
    assert settings.agent_max_iterations == 40
    assert settings.agent_dev_mode is True
    assert settings.llm_history_turns == 40
    assert settings.embedding_model == "text-embedding-v3"
    assert settings.embedding_api_key == "sk-dashscope"


def test_env_overrides_config_toml(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///config-toml.db")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm.main]
model = "from-toml"
base_url = "https://toml.example/v1"
api_key = "toml-key"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_TOML_PATH", str(config_path))
    monkeypatch.setenv("LLM_MODEL", "from-env")

    settings = Settings(_env_file=None)

    assert settings.llm_model == "from-env"
    assert settings.llm_base_url == "https://toml.example/v1"


def test_config_toml_overrides_dotenv_defaults(monkeypatch, tmp_path):
    _clear_model_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm.main]
model = "from-toml"
api_key = "${MISSING_LLM_KEY}"
""",
        encoding="utf-8",
    )
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        """
DATABASE_URL=sqlite:///dotenv-defaults.db
LLM_MODEL=from-dotenv
LLM_API_KEY=dotenv-key
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_TOML_PATH", str(config_path))

    settings = Settings(_env_file=str(dotenv_path))

    assert settings.database_url == "sqlite:///dotenv-defaults.db"
    assert settings.llm_model == "from-toml"
    assert settings.llm_api_key == ""


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
