import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_ENV_FILE = PROJECT_ROOT / ".env"


def _default_runtime_root() -> Path:
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "CaiBao"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CaiBao"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "CaiBao"
    return Path.home() / ".local" / "share" / "CaiBao"


DEFAULT_RUNTIME_ROOT = _default_runtime_root()
DEFAULT_UPLOAD_ROOT_DIR = str(DEFAULT_RUNTIME_ROOT / "uploads")


class Settings(BaseSettings):
    app_name: str = "CaiBao"
    app_version: str = "0.23.2"
    app_env: str = "dev"
    api_prefix: str = "/api/v1"
    database_url: str
    db_connect_timeout_seconds: int = 5
    auth_jwt_secret: str = "dev-auth-secret-change-me"
    auth_jwt_algorithm: str = "HS256"
    auth_access_token_ttl_minutes: int = 15
    auth_refresh_token_ttl_days: int = 14
    auth_cookie_secure: bool = False
    auth_cookie_domain: str | None = None
    auth_cookie_samesite: str = "lax"
    auth_access_cookie_name: str = "caibao_access_token"
    auth_refresh_cookie_name: str = "caibao_refresh_token"
    dev_admin_enabled: bool = True
    dev_admin_account_id: str = "dev_admin"
    dev_admin_display_name: str = "Developer Admin"
    dev_admin_token: str = "dev-admin-token"

    llm_provider: str = "mock"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 30.0
    llm_history_turns: int = 6
    llm_history_mode: str = "auto"

    embedding_provider: str = "mock"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_mock_dim: int = 256
    embedding_batch_size: int = 32
    embedding_timeout_seconds: float = 30.0
    upload_root_dir: str = DEFAULT_UPLOAD_ROOT_DIR
    upload_max_file_size_mb: int = 20
    db_legacy_init_enabled: bool | None = None

    # Web tools
    web_tools_enabled: bool = True
    web_search_provider: str = "disabled"
    web_search_api_key: str | None = None
    web_fetch_max_bytes: int = 1_000_000
    web_fetch_block_private_ips: bool = True

    # File tools
    file_tools_enabled: bool = True
    file_tools_root_dir: str = "data/agent_files"

    # Shell tools
    shell_tool_enabled: bool = False
    shell_allowed_cwd: str = "data/agent_shell"
    shell_default_timeout_seconds: int = 20
    shell_max_timeout_seconds: int = 120
    shell_max_output_bytes: int = 200_000
    shell_allow_network: bool = False

    # MCP
    mcp_enabled: bool = False
    mcp_config_path: str = "config/mcp_servers.json"
    mcp_init_timeout_seconds: float = 10.0
    mcp_call_timeout_seconds: float = 30.0
    mcp_max_output_bytes: int = 200_000

    # Markdown memory
    memory_markdown_enabled: bool = True
    memory_root_dir: str = "data/memory"
    memory_consolidation_min_turns: int = 4
    memory_recent_turns: int = 6
    memory_optimizer_enabled: bool = False
    memory_optimizer_interval_seconds: int = 64800

    # Retrieval enhancement
    retrieval_enhancement_enabled: bool = True
    retrieval_query_rewrite_enabled: bool = True
    retrieval_hyde_enabled: bool = True
    retrieval_sufficiency_enabled: bool = True
    retrieval_fast_timeout_ms: int = 5000

    # Plugin system
    plugin_enabled: bool = False
    plugin_dirs: str = "data/plugins"
    plugin_fail_fast: bool = False

    # Proactive push
    proactive_enabled: bool = False
    proactive_scheduler_enabled: bool = False
    proactive_tick_interval_seconds: int = 60
    proactive_energy_urgency_weight: float = 0.4
    proactive_energy_relevance_weight: float = 0.35
    proactive_energy_fatigue_weight: float = 0.25
    proactive_outbound_channels: str = "database"
    proactive_webhook_url: str = ""
    proactive_max_retries: int = 3

    # Drift
    drift_enabled: bool = False
    drift_root_dir: str = "data/drift"

    # SubAgent
    subagent_enabled: bool = False
    subagent_sync_max_steps: int = 10
    subagent_async_max_steps: int = 15
    peer_agent_enabled: bool = False
    peer_agent_launch_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=str(ROOT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Keep standard priority: init args > env vars > .env > file secrets.
        # Root .env path is fixed via model_config.env_file above.
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @field_validator("auth_cookie_domain", mode="before")
    @classmethod
    def _normalize_auth_cookie_domain(cls, value):
        if value == "":
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
