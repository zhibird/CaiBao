"""Exercise backend switches across real ownership, history and worker boundaries.

Only the provider HTTP transport and the external Pi executable are local fakes.
Prompt assembly, routing, authentication, persistence, subprocess stdin and native
session checkpointing remain real. No real model account or credentials are used.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import stat
import sys

import httpx
import pytest

from app.api import deps
from app.core.config import reload_settings
from app.services.agent_service import AgentService
from app.services.llm_service import LLMService
from app.services.memory_markdown_store import MemoryMarkdownStore
from tests.auth_helpers import (
    login_existing_user,
    register_workspace_user,
    register_workspace_user_in_team,
)


_PI_PROGRAM = r'''
import json, os, sys, uuid

def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)

argv = sys.argv[1:]
prompt = sys.stdin.read()
behavior = json.loads(open(os.environ['CAIBAO_TEST_PI_BEHAVIOR']).read())
session = argv[argv.index('--session') + 1] if '--session' in argv else 'native-' + uuid.uuid4().hex
with open(os.environ['CAIBAO_TEST_PI_CAPTURE'], 'a') as capture:
    capture.write(json.dumps({'argv': argv, 'stdin': prompt, 'session': session, 'cwd': os.getcwd()}) + '\n')
emit({'type': 'session', 'id': session})
if behavior['outcome'] == 'crash':
    emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'PARTIAL_UNCONFIRMED'}})
    sys.exit(3)
if behavior['outcome'] == 'unrecognized':
    sys.exit(0)
if behavior['outcome'] == 'failed':
    message = {'role': 'assistant', 'content': [], 'stopReason': 'error', 'errorMessage': 'FAKE_PI_FAILED_TURN'}
else:
    message = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'PI_NATIVE_FORMAL_REPLY'}], 'stopReason': 'stop'}
emit({'type': 'message_end', 'message': message})
emit({'type': 'agent_end', 'messages': [message]})
'''


@pytest.fixture
def continuity_boundary(client, monkeypatch, tmp_path):
    script = tmp_path / "fake_pi.py"
    script.write_text(_PI_PROGRAM, encoding="utf-8")
    executable = tmp_path / "fake_pi"
    executable.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    capture = tmp_path / "pi-inputs.jsonl"
    behavior = tmp_path / "pi-behavior.json"
    behavior.write_text('{"outcome":"success"}')
    workspace = tmp_path / "workspaces"
    values = {
        "CLI_AGENT_ENABLED": "true", "CLI_AGENT_PI_PATH": str(executable),
        "CLI_AGENT_PI_MODEL": "pi-configured-model", "CLI_AGENT_PI_PROVIDER": "pi-local-provider",
        "CLI_AGENT_PI_THINKING": "", "CLI_AGENT_WORKSPACE_ROOT": str(workspace),
        "CLI_AGENT_EXTRA_ENV_JSON": json.dumps({
            "CAIBAO_TEST_PI_CAPTURE": str(capture), "CAIBAO_TEST_PI_BEHAVIOR": str(behavior),
        }),
        "LLM_PROVIDER": "openai", "LLM_MODEL": "api-configured-model",
        "LLM_BASE_URL": "https://continuity.invalid/v1", "LLM_API_KEY": "fake-local-only-key",
        "LLM_HISTORY_TURNS": "12", "MEMORY_MARKDOWN_ENABLED": "true",
        "MEMORY_ROOT_DIR": str(tmp_path / "memory"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    reload_settings()
    store = MemoryMarkdownStore(root_dir=str(tmp_path / "memory"))
    monkeypatch.setattr(deps, "_memory_store_singleton", store)
    monkeypatch.setattr(deps._get_consolidation_service(), "_store", store)
    api_calls = []

    def capture_payload(payload):
        api_calls.append(copy.deepcopy(payload))
        return f"API_FORMAL_REPLY_{len(api_calls)}"

    def fake_post(self, payload, **_kwargs):
        answer = capture_payload(payload)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        }, request=httpx.Request("POST", "https://continuity.invalid/v1/chat/completions"))

    @contextmanager
    def fake_stream(self, payload, **_kwargs):
        answer = capture_payload(payload)
        events = [
            {"choices": [{"delta": {"content": answer}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        text = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        yield httpx.Response(200, text=text, request=httpx.Request(
            "POST", "https://continuity.invalid/v1/chat/completions",
        ))

    monkeypatch.setattr(LLMService, "_post_chat_completion", fake_post)
    monkeypatch.setattr(LLMService, "_post_chat_completion_stream", fake_stream)
    team_id, user_id = register_workspace_user(
        client, prefix="continuity", display_name="Continuity owner",
    )
    conversation = _conversation(client)
    return {
        "capture": capture, "behavior": behavior, "workspace": workspace,
        "api_calls": api_calls, "team_id": team_id, "user_id": user_id,
        "conversation": conversation,
    }


def _conversation(client):
    response = client.post("/api/v1/conversations", json={"title": "Backend continuity"})
    assert response.status_code == 201, response.text
    return response.json()


def _run(client, conversation, task, *, mode="agent_auto", expected="completed", **changes):
    payload = {
        "conversation_id": conversation["conversation_id"], "task": task,
        "run_mode": mode, "enabled_tools": [], "include_memory": False,
        "include_library": False,
    }
    payload.update(changes)
    queued = client.post("/api/v1/agent/runs", json=payload)
    assert queued.status_code == 200, queued.text
    run_id = queued.json()["run_id"]
    observed = client.get(f"/api/v1/agent/runs/{run_id}/stream")
    assert observed.status_code == 200, observed.text
    response = client.get(f"/api/v1/agent/runs/{run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected, response.text
    return response.json()


def _pi_calls(boundary):
    path = boundary["capture"]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _api_text(payload):
    return json.dumps(payload["messages"], ensure_ascii=False)


def _memory_document(client, conversation, marker):
    notebook = client.get("/api/v1/companion/memory", params={
        "conversation_id": conversation["conversation_id"],
    })
    assert notebook.status_code == 200, notebook.text
    document = next(item for item in notebook.json()["documents"] if item["kind"] == "long_term")
    changed = client.put("/api/v1/companion/memory/documents/long_term", json={
        "conversation_id": conversation["conversation_id"], "content": marker,
        "expected_revision": document["revision"],
    })
    assert changed.status_code == 200, changed.text


def test_api_pi_api_pi_shares_formal_history_without_repeating_native_turns(client, continuity_boundary):
    boundary = continuity_boundary
    conversation = boundary["conversation"]
    api_one = _run(client, conversation, "API_FIRST_REQUIREMENT_719")
    assert api_one["answer"].startswith("API_FORMAL_REPLY_")
    first_payload = boundary["api_calls"][-1]
    assert _api_text(first_payload).count("API_FIRST_REQUIREMENT_719") == 1
    memory_marker = "PRIVATE_LONG_TERM_MEMORY_MUST_STAY_OUT_OF_CLI_719"
    _memory_document(client, conversation, memory_marker)

    pi_one = _run(client, conversation, "PI_FIRST_CURRENT_TASK_719", mode="pi", include_memory=True)
    first_pi = _pi_calls(boundary)[0]
    assert "--session" not in first_pi["argv"]
    assert first_pi["stdin"].count("PI_FIRST_CURRENT_TASK_719") == 1
    assert first_pi["stdin"].count("API_FIRST_REQUIREMENT_719") == 1
    assert api_one["answer"] in first_pi["stdin"]
    assert memory_marker not in first_pi["stdin"]
    assert memory_marker not in " ".join(first_pi["argv"])
    assert pi_one["execution_identity"]["backend"] == "pi"
    assert pi_one["execution_identity"]["model"] == "pi-configured-model"
    assert pi_one["model"] == "pi-configured-model"
    assert pi_one["model_routes"] is None
    assert first_pi["session"] not in json.dumps({
        "execution_identity": pi_one["execution_identity"], "context_snapshot": pi_one["context_snapshot"],
    })

    api_two = _run(client, conversation, "API_SECOND_REQUIREMENT_719")
    second_payload = boundary["api_calls"][-1]
    second_api_text = _api_text(second_payload)
    for marker in ("API_FIRST_REQUIREMENT_719", "PI_FIRST_CURRENT_TASK_719", "PI_NATIVE_FORMAL_REPLY"):
        assert second_api_text.count(marker) == 1
    assert second_api_text.index("API_FIRST_REQUIREMENT_719") < second_api_text.index("PI_FIRST_CURRENT_TASK_719")
    assert second_api_text.count("API_SECOND_REQUIREMENT_719") == 1
    assert second_payload["model"] == "api-configured-model"
    assert api_two["execution_identity"]["backend"] == "api"
    assert api_two["execution_identity"]["model"] == "api-configured-model"

    _run(client, conversation, "PI_SECOND_CURRENT_TASK_719", mode="pi", include_memory=True)
    second_pi = _pi_calls(boundary)[1]
    assert second_pi["argv"][second_pi["argv"].index("--session") + 1] == first_pi["session"]
    assert second_pi["stdin"].count("PI_SECOND_CURRENT_TASK_719") == 1
    assert "API_SECOND_REQUIREMENT_719" in second_pi["stdin"]
    assert api_two["answer"] in second_pi["stdin"]
    assert "PI_FIRST_CURRENT_TASK_719" not in second_pi["stdin"]
    assert "PI_NATIVE_FORMAL_REPLY" not in second_pi["stdin"]
    assert "API_FIRST_REQUIREMENT_719" not in second_pi["stdin"]
    assert memory_marker not in second_pi["stdin"]
    assert len(_pi_calls(boundary)) == 2


def test_cli_execution_does_not_resolve_api_routes_or_pretend_requested_api_model(client, continuity_boundary, monkeypatch):
    def api_route_must_not_run(*_args, **_kwargs):
        raise AssertionError("CLI execution must not consult API routes")
    monkeypatch.setattr(AgentService, "_resolve_routes", api_route_must_not_run)
    run = _run(
        client, continuity_boundary["conversation"], "CLI_ROUTE_INDEPENDENT_219", mode="pi",
        model="api-requested-but-ignored-model", model_routes={"planner": "invalid-api-model"},
    )
    assert run["execution_identity"]["backend"] == "pi"
    assert run["execution_identity"]["model"] == "pi-configured-model"
    assert run["execution_identity"]["requested_model"] == "api-requested-but-ignored-model"
    assert run["model_routes"] is None
    assert continuity_boundary["api_calls"] == []


@pytest.mark.parametrize("change", ["model", "system_prompt"])
def test_declared_model_or_role_change_starts_fresh_native_context(client, continuity_boundary, monkeypatch, change):
    boundary = continuity_boundary
    conversation = boundary["conversation"]
    first = _run(client, conversation, "BEFORE_REBIND_TASK_519", mode="pi", system_prompt="ROLE_ONE_519")
    changes = {"system_prompt": "ROLE_ONE_519"}
    if change == "model":
        monkeypatch.setenv("CLI_AGENT_PI_MODEL", "pi-new-configured-model")
        reload_settings()
    else:
        changes["system_prompt"] = "ROLE_TWO_519"
    second = _run(client, conversation, "AFTER_REBIND_TASK_519", mode="pi", **changes)
    before, after = _pi_calls(boundary)
    assert "--session" not in after["argv"]
    assert after["session"] != before["session"]
    assert "BEFORE_REBIND_TASK_519" in after["stdin"]
    assert after["stdin"].count("AFTER_REBIND_TASK_519") == 1
    assert first["execution_identity"]["binding"] != second["execution_identity"]["binding"]
    assert "ROLE_ONE_519" not in json.dumps(first)
    assert changes["system_prompt"] not in json.dumps(second)


@pytest.mark.parametrize("outcome", ["failed", "crash", "unrecognized"])
def test_failed_or_unknown_cli_turn_is_not_resumed_or_automatically_replayed(client, continuity_boundary, outcome):
    boundary = continuity_boundary
    conversation = boundary["conversation"]
    _run(client, conversation, "LAST_CONFIRMED_PI_TASK_819", mode="pi")
    boundary["behavior"].write_text(json.dumps({"outcome": outcome}))
    failed = _run(client, conversation, "AMBIGUOUS_PI_TASK_819", mode="pi", expected="failed")
    calls_before_reobservation = len(_pi_calls(boundary))
    replay = client.get(f"/api/v1/agent/runs/{failed['run_id']}/stream")
    assert replay.status_code == 200
    started_again = client.post(f"/api/v1/agent/runs/{failed['run_id']}/start")
    assert started_again.status_code == 200, started_again.text
    assert len(_pi_calls(boundary)) == calls_before_reobservation == 2

    boundary["behavior"].write_text('{"outcome":"success"}')
    _run(client, conversation, "NEW_USER_PI_TASK_819", mode="pi")
    latest = _pi_calls(boundary)[-1]
    assert "--session" not in latest["argv"]
    assert "LAST_CONFIRMED_PI_TASK_819" in latest["stdin"]
    assert "AMBIGUOUS_PI_TASK_819" in latest["stdin"]
    assert "【此前任务失败，未确认完成】" in latest["stdin"]
    assert latest["stdin"].count("NEW_USER_PI_TASK_819") == 1
    assert len(_pi_calls(boundary)) == 3


def test_context_and_native_sessions_are_isolated_by_conversation_and_user(client, continuity_boundary):
    boundary = continuity_boundary
    original = boundary["conversation"]
    _run(client, original, "OWNER_ONLY_CONVERSATION_SECRET_319")
    _run(client, original, "OWNER_NATIVE_TURN_319", mode="pi")
    first_pi = _pi_calls(boundary)[0]
    other_conversation = _conversation(client)
    _run(client, other_conversation, "SAME_OWNER_FRESH_CONVERSATION_319", mode="pi")
    second_pi = _pi_calls(boundary)[1]
    assert "--session" not in second_pi["argv"]
    assert "OWNER_ONLY_CONVERSATION_SECRET_319" not in second_pi["stdin"]
    assert second_pi["cwd"] != first_pi["cwd"]

    _, second_user = register_workspace_user_in_team(
        client, team_id=boundary["team_id"], prefix="continuity_other", display_name="Other member",
    )
    forbidden = client.post("/api/v1/agent/runs", json={
        "conversation_id": original["conversation_id"], "task": "DO_NOT_OPEN_PRIVATE_SESSION_319", "run_mode": "pi",
    })
    assert forbidden.status_code == 404, forbidden.text
    own_conversation = _conversation(client)
    _run(client, own_conversation, "OTHER_USER_FRESH_CONVERSATION_319", mode="pi")
    third_pi = _pi_calls(boundary)[2]
    assert "--session" not in third_pi["argv"]
    assert "OWNER_ONLY_CONVERSATION_SECRET_319" not in third_pi["stdin"]
    assert third_pi["cwd"] not in {first_pi["cwd"], second_pi["cwd"]}

    login_existing_user(client, user_id=boundary["user_id"])
    _run(client, original, "OWNER_BACK_TO_ORIGINAL_319", mode="pi")
    resumed = _pi_calls(boundary)[3]
    assert resumed["argv"][resumed["argv"].index("--session") + 1] == first_pi["session"]
    assert "OTHER_USER_FRESH_CONVERSATION_319" not in resumed["stdin"]
    assert "SAME_OWNER_FRESH_CONVERSATION_319" not in resumed["stdin"]
    assert second_user != boundary["user_id"]
