"""Runtime selection is explicit; listing never starts a CLI or probes login."""

import json
from uuid import uuid4

import pytest

from app.core.config import reload_settings
from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.services.llm_service import LLMService
from app.services.rag_chat_service import RagChatService
from tests.auth_helpers import register_workspace_user
from tests.test_cli_agent import fake_pi_path  # noqa: F401 - reusable executable fixture


def _register(client):
    return register_workspace_user(client, prefix=f"backend_{uuid4().hex[:6]}", display_name="Runtime Friend")


def _catalogue(client):
    response = client.get("/api/v1/companion/backends")
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()}


def test_backend_catalogue_requires_login(client):
    assert client.get("/api/v1/companion/backends").status_code == 401


def test_disabled_cli_catalogue_is_honest_and_does_not_probe(client, monkeypatch):
    monkeypatch.setenv("CLI_AGENT_ENABLED", "false")
    monkeypatch.setenv("CLI_AGENT_CODEX_PATH", "/private/example/path/codex")
    reload_settings()
    _register(client)

    def no_executable_probe(_path):
        pytest.fail("Disabled backends should not inspect installed CLIs")

    monkeypatch.setattr("app.services.companion_service.shutil.which", no_executable_probe)
    catalogue = _catalogue(client)
    assert list(catalogue) == ["api", "codex", "claude_code", "pi"]
    assert catalogue["api"]["available"] is True
    assert catalogue["api"]["demo_mode"] is True
    assert "mock" in catalogue["api"]["reason"]
    for key in ("codex", "claude_code", "pi"):
        assert catalogue[key]["available"] is False
        assert catalogue[key]["kind"] == "cli"
        assert "未启用" in catalogue[key]["reason"]
    assert "/private/example/path" not in json.dumps(catalogue)


def test_catalogue_checks_executable_only_and_does_not_claim_login(client, monkeypatch):
    monkeypatch.setenv("CLI_AGENT_ENABLED", "true")
    monkeypatch.setenv("CLI_AGENT_CODEX_PATH", "test-codex")
    monkeypatch.setenv("CLI_AGENT_CLAUDE_PATH", "test-claude")
    monkeypatch.setenv("CLI_AGENT_PI_PATH", "test-pi")
    monkeypatch.setenv("CLI_AGENT_CODEX_MODEL", "configured-codex-model")
    reload_settings()
    _register(client)
    probes = []

    def which(path):
        probes.append(path)
        return "/installed/codex" if path == "test-codex" else None

    monkeypatch.setattr("app.services.companion_service.shutil.which", which)
    catalogue = _catalogue(client)
    assert probes == ["test-codex", "test-claude", "test-pi"]
    assert catalogue["codex"]["available"] is True
    assert catalogue["codex"]["model"] == "configured-codex-model"
    assert "尚未验证" in catalogue["codex"]["reason"]
    assert "登录" in catalogue["codex"]["reason"]
    assert catalogue["claude_code"]["available"] is False
    assert "未找到" in catalogue["claude_code"]["reason"]
    assert catalogue["pi"]["model"] is None


def test_api_catalogue_never_exposes_credentials_or_claims_connectivity(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "private-test-key-do-not-expose")
    monkeypatch.setenv("LLM_MODEL", "test-api-model")
    reload_settings()
    _register(client)
    catalogue = _catalogue(client)
    assert catalogue["api"]["available"] is True
    assert catalogue["api"]["model"] == "test-api-model"
    assert catalogue["api"]["demo_mode"] is False
    assert "尚未验证" in catalogue["api"]["reason"]
    assert "private-test-key" not in json.dumps(catalogue)


@pytest.mark.parametrize("enabled", [False, True])
def test_unavailable_cli_fails_before_queuing(client, monkeypatch, enabled):
    monkeypatch.setenv("CLI_AGENT_ENABLED", str(enabled).lower())
    monkeypatch.setenv("CLI_AGENT_CODEX_PATH", "/definitely-missing/caibao-codex")
    reload_settings()
    team_id, user_id = _register(client)
    response = client.post("/api/v1/companion/runs", json={"task": "整理项目", "backend": "codex"})
    assert response.status_code == 400
    assert ("未找到" if enabled else "未启用") in response.json()["detail"]
    with SessionLocal() as db:
        assert db.query(AgentRun).filter_by(team_id=team_id, user_id=user_id).count() == 0


def test_cli_cannot_silently_become_chat_or_use_an_api_model_override(client, monkeypatch):
    _register(client)
    for extras in ({"mode": "chat"}, {"model": "api-only-model"}):
        response = client.post("/api/v1/companion/runs", json={"task": "hello", "backend": "pi", **extras})
        assert response.status_code == 400


def test_backend_selection_rejects_unknown_runtime_and_execution_policy(client):
    _register(client)
    for extras in (
        {"backend": "custom-shell"}, {"backend": "codex", "path": "/bin/sh"},
        {"backend": "pi", "permission_mode": "bypassPermissions"},
        {"backend": "codex", "run_mode": "agent_auto"},
    ):
        response = client.post("/api/v1/companion/runs", json={"task": "hello", **extras})
        assert response.status_code == 422


def test_companion_pi_stream_uses_cli_model_and_no_caibao_retrieval(
    client, monkeypatch, fake_pi_path, tmp_path,
):
    argv_dump = tmp_path / "argv.json"
    monkeypatch.setenv("CLI_AGENT_ENABLED", "true")
    monkeypatch.setenv("CLI_AGENT_PI_PATH", fake_pi_path)
    monkeypatch.setenv("CLI_AGENT_PI_MODEL", "configured-pi-model")
    monkeypatch.setenv("CLI_AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("CLI_AGENT_EXTRA_ENV_JSON", json.dumps({"FAKE_PI_ARGV_DUMP": str(argv_dump)}))
    reload_settings()
    team_id, user_id = _register(client)
    conversation = client.post("/api/v1/conversations", json={"title": "Companion CLI"}).json()
    assert _catalogue(client)["pi"]["available"] is True

    def no_api_work(*args, **kwargs):
        pytest.fail("CLI runtime must not make a hidden API-model or RAG call")

    monkeypatch.setattr(LLMService, "complete_chat", no_api_work)
    monkeypatch.setattr(RagChatService, "ask", no_api_work)
    start = client.post("/api/v1/companion/runs", json={
        "task": "帮我整理一下", "backend": "pi", "mode": "agent",
        "conversation_id": conversation["conversation_id"], "include_memory": True,
    })
    assert start.status_code == 201
    run_id = start.json()["run_id"]
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        request = json.loads(run.request_payload_json)
        assert (run.team_id, run.user_id, run.trigger_channel) == (team_id, user_id, "companion")
        assert request["run_mode"] == "pi"
        assert request["include_memory"] is False
        assert request["include_library"] is False
        assert request["enabled_tools"] == []
        assert request["model"] is None
        assert request["confirm_dangerous_actions"] is False
        assert "彩宝" in request["system_prompt"]

    stream = client.get(f"/api/v1{start.json()['stream_url']}")
    assert stream.status_code == 200
    for event in ("run.started", "llm.delta", "tool.started", "tool.result", "run.completed"):
        assert f"event: {event}" in stream.text
    final = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert final["status"] == "completed"
    assert final["answer"] == "最终答案"
    invocation = json.loads(argv_dump.read_text())
    assert invocation["argv"][invocation["argv"].index("--model") + 1] == "configured-pi-model"
    assert "彩宝" in invocation["argv"][invocation["argv"].index("--append-system-prompt") + 1]
    assert invocation["stdin"] == "帮我整理一下"
    history = client.get("/api/v1/chat/history", params={"conversation_id": conversation["conversation_id"]}).json()
    assert history["items"][0]["channel"] == "companion"
