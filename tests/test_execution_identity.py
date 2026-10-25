from __future__ import annotations

from dataclasses import replace
import json

import pytest

from app.core.config import Settings
from app.core.exceptions import DomainValidationError
from app.services.cli_session_store import build_cli_session_binding
from app.services.execution_identity import (
    build_api_execution_identity,
    build_cli_execution_identity,
    safe_execution_endpoint,
)
from app.services.llm_router import ModelRoute


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://alice:password@api.example/v1?api_key=query-secret#private", "https://api.example/v1"),
        ("http://user:password@[::1]:8080/v1?token=secret", "http://[::1]:8080/v1"),
        (" HTTPS://EXAMPLE.COM:443/v1/ ", "https://example.com:443/v1/"),
        ("https://api.example:invalid/v1?token=secret", None),
        ("https://[invalid", None),
        ("file:///secret/key", None),
        ("not-a-url?api_key=secret", None),
        (None, None),
    ],
)
def test_public_endpoint_never_exposes_url_credentials(endpoint, expected):
    assert safe_execution_endpoint(endpoint) == expected


def test_api_identity_uses_live_resolved_route_without_persisting_credential_material():
    route = ModelRoute(
        model_name="actual-model", base_url="https://user:private-pass@api.example/v1?key=url-secret#private-fragment",
        api_key="private-api-credential", source="user_config",
    )
    identity = build_api_execution_identity({"planner": route}, requested_model="model-alias", provider="openai")
    serialized = json.dumps(identity)

    assert identity["model"] == "actual-model"
    assert identity["requested_model"] == "model-alias"
    assert identity["model_source"] == "resolved"
    assert identity["endpoint"] == "https://api.example/v1"
    assert identity["binding"].startswith("v1:")
    for private_value in ("user:", "private-pass", "url-secret", "private-fragment", "private-api-credential", "credential_hash"):
        assert private_value not in serialized


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "second-key"},
        {"base_url": "https://second.example/v1"},
        {"base_url": "https://api.example/v1?route=second"},
        {"base_url": "https://second-user:second-pass@api.example/v1"},
        {"model_name": "second-model"},
    ],
)
def test_api_binding_detects_changed_model_connection_or_credential(change):
    route = ModelRoute(model_name="first-model", base_url="https://api.example/v1", api_key="first-key")
    before = build_api_execution_identity({"planner": route})
    after = build_api_execution_identity({"planner": replace(route, **change)})
    assert before["binding"] != after["binding"]


def test_api_binding_covers_all_roles_but_not_display_metadata_or_requested_alias():
    first = ModelRoute(model_name="planner-model", base_url="https://api.example/v1", api_key="key")
    fast = ModelRoute(model_name="fast-model", base_url="https://api.example/v1", api_key="key")
    original = build_api_execution_identity({"planner": first, "fast": fast}, requested_model="first-alias")
    equivalent = build_api_execution_identity(
        {"fast": fast, "planner": replace(first, source="different-resolver-label")}, requested_model="second-alias",
    )
    changed_fast = build_api_execution_identity({"planner": first, "fast": replace(fast, model_name="new-fast-model")})
    changed_provider = build_api_execution_identity({"planner": first, "fast": fast}, provider="different-provider")
    assert original["binding"] == equivalent["binding"]
    assert original["binding"] != changed_fast["binding"]
    assert original["binding"] != changed_provider["binding"]


def test_dictionary_routes_match_live_route_identity():
    route = ModelRoute(model_name="chosen", base_url="https://api.example/v1", api_key="private", source="default")
    objects = build_api_execution_identity({"planner": route}, requested_model="chosen")
    dictionaries = build_api_execution_identity({"planner": {
        "model_name": route.model_name, "base_url": route.base_url,
        "api_key": route.api_key, "source": route.source,
    }}, requested_model="chosen")
    assert objects == dictionaries
    assert objects["model_source"] == "requested"


@pytest.mark.parametrize("mode", ["claude_code", "codex", "pi"])
def test_cli_without_declared_model_does_not_claim_api_or_ignored_request_model(mode):
    settings = Settings(
        _env_file=None, llm_model="api-default",
        cli_agent_claude_model="", cli_agent_codex_model="", cli_agent_pi_model="",
    )
    identity = build_cli_execution_identity(settings, mode, requested_model="ignored-api-selection")
    assert identity["model"] is None
    assert identity["model_source"] == "runtime_default"
    assert identity["backend"] == mode
    assert identity["endpoint"] is None
    assert "api-default" not in json.dumps(identity)
    assert identity["binding"] == build_cli_session_binding(settings, mode)


@pytest.mark.parametrize(
    ("mode", "setting_name"),
    [("claude_code", "cli_agent_claude_model"), ("codex", "cli_agent_codex_model"), ("pi", "cli_agent_pi_model")],
)
def test_cli_snapshot_matches_its_actual_command_model_and_session_binding(mode, setting_name):
    settings = Settings(_env_file=None, **{setting_name: "cli-chosen-model"}, cli_agent_pi_provider="local-provider")
    identity = build_cli_execution_identity(
        settings, mode, system_prompt="private personality prompt", requested_model="api-chosen", dry_run=True,
    )
    assert identity["model"] == "cli-chosen-model"
    assert identity["model_source"] == "configured"
    assert identity["provider"] == ("local-provider" if mode == "pi" else None)
    assert identity["binding"] == build_cli_session_binding(
        settings, mode, dry_run=True, system_prompt="private personality prompt",
    )
    assert identity["binding"] != build_cli_execution_identity(settings, mode)["binding"]
    assert "private personality prompt" not in json.dumps(identity)


def test_cli_identity_omits_private_environment_and_opaque_external_defaults():
    settings = Settings(
        _env_file=None, cli_agent_extra_env_json=json.dumps({
            "PRIVATE_API_KEY": "private-key-value", "PRIVATE_TOKEN": "private-token-value",
            "OPENAI_BASE_URL": "https://user:pass@private.example/v1?secret=query",
        }),
    )
    identity = build_cli_execution_identity(settings, "codex")
    public = json.dumps(identity)
    for private in ("private-key-value", "private-token-value", "private.example", "user:pass", "query"):
        assert private not in public


def test_cli_unknown_backend_rejects_instead_of_falling_back_to_api():
    with pytest.raises(DomainValidationError):
        build_cli_execution_identity(Settings(_env_file=None), "unknown")
