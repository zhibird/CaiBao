"""A real model proposal keeps the same execution address across approval.

Only the provider HTTP transport is replaced. Admission, worker ownership, route
resolution, streaming proposal parsing, approval tokens and memory writes run
through the production authenticated HTTP interfaces. Credentials are local fakes.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import reload_settings
from app.db.session import SessionLocal
from app.models.memory_card import MemoryCard
from app.services.llm_service import LLMService
from tests.auth_helpers import register_workspace_user


@pytest.fixture
def continuation_boundary(client, monkeypatch):
    original = {
        "LLM_PROVIDER": "openai", "LLM_MODEL": "original-approval-brain",
        "LLM_BASE_URL": "https://original-approval.invalid/v1", "LLM_API_KEY": "local-original-approval-key",
        "LLM_FAST_MODEL": "", "LLM_VL_MODEL": "", "LLM_FAST_BASE_URL": "", "LLM_VL_BASE_URL": "",
        "LLM_FAST_API_KEY": "", "LLM_VL_API_KEY": "", "MEMORY_MARKDOWN_ENABLED": "false",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    reload_settings()
    team_id, user_id = register_workspace_user(client, prefix="frozen_approval", display_name="Approval owner")
    response = client.post("/api/v1/conversations", json={"title": "Frozen execution approval"})
    assert response.status_code == 201, response.text
    conversation = response.json()
    arguments = {"title": "批准后才保存", "content": "只有原本的模型配置恢复后，才可保存这条记忆。"}
    calls = []

    def capture(kind, payload, kwargs):
        calls.append({"kind": kind, "payload": copy.deepcopy(payload), **kwargs})

    @contextmanager
    def proposal(self, payload, **kwargs):
        capture("stream", payload, kwargs)
        events = [
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "original-approved-operation", "type": "function",
                "function": {"name": "create_memory_card", "arguments": json.dumps(arguments, ensure_ascii=False)},
            }]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        body = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events) + "data: [DONE]\n\n"
        yield httpx.Response(200, text=body, request=httpx.Request("POST", "https://local-fake.invalid/completion"))

    def conclusion(self, payload, **kwargs):
        capture("complete", payload, kwargs)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "原操作已经返回。"}, "finish_reason": "stop"}],
        }, request=httpx.Request("POST", "https://local-fake.invalid/completion"))

    monkeypatch.setattr(LLMService, "_post_chat_completion_stream", proposal)
    monkeypatch.setattr(LLMService, "_post_chat_completion", conclusion)
    return {
        "original": original, "team_id": team_id, "user_id": user_id,
        "conversation": conversation, "calls": calls, "arguments": arguments,
    }


def _propose(client, boundary, **changes):
    queued = client.post("/api/v1/agent/runs", json={
        "task": "请保存这条记忆，等我批准才执行。", "trigger_channel": "companion",
        "conversation_id": boundary["conversation"]["conversation_id"],
        "enabled_tools": ["create_memory_card"], "include_memory": False,
        "include_library": False, **changes,
    })
    assert queued.status_code == 200, queued.text
    run_id = queued.json()["run_id"]
    observed = client.get(f"/api/v1/agent/runs/{run_id}/stream")
    assert observed.status_code == 200, observed.text
    run = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert run["status"] == "requires_confirmation", run
    assert len(run["required_confirmations"]) == 1
    assert run["required_confirmations"][0]["tool_name"] == "create_memory_card"
    assert run["required_confirmations"][0]["arguments"]["space_id"] == boundary["conversation"]["space_id"]
    assert run["approval_token"]
    assert _memories(boundary) == []
    assert len([call for call in boundary["calls"] if call["kind"] == "stream"]) == 1
    boundary["proposal_call_count"] = len(boundary["calls"])
    return run


def _confirm(client, run):
    return client.post(f"/api/v1/agent/runs/{run['run_id']}/confirm", json={"approval_token": run["approval_token"]})


def _memories(boundary):
    with SessionLocal() as db:
        return list(db.scalars(select(MemoryCard).where(
            MemoryCard.team_id == boundary["team_id"], MemoryCard.user_id == boundary["user_id"],
        )))


def _assert_rejected_without_effect(client, boundary, pending):
    rejected = _confirm(client, pending)
    assert rejected.status_code == 400, rejected.text
    assert "配置已变化" in rejected.json()["detail"]
    current = client.get(f"/api/v1/agent/runs/{pending['run_id']}").json()
    assert current["status"] == "requires_confirmation"
    for key in ("approval_token", "required_confirmations", "execution_identity", "context_snapshot", "model_routes"):
        assert current[key] == pending[key]
    assert len(boundary["calls"]) == boundary["proposal_call_count"]
    assert _memories(boundary) == []


def _assert_original_approved(client, boundary, pending):
    result = _confirm(client, pending)
    assert result.status_code == 200, result.text
    completed = result.json()
    assert completed["status"] == "completed", completed
    assert completed["execution_identity"] == pending["execution_identity"]
    assert completed["context_snapshot"] == pending["context_snapshot"]
    assert completed["required_confirmations"] == []
    assert len(boundary["calls"]) == boundary["proposal_call_count"] + 1
    first = next(call for call in boundary["calls"] if call["kind"] == "stream")
    final = boundary["calls"][-1]
    assert first["kind"] == "stream" and final["kind"] == "complete"
    for key in ("base_url", "api_key"):
        assert final[key] == first[key]
    assert final["payload"]["model"] == first["payload"]["model"] == pending["execution_identity"]["model"]
    tools = [message for message in final["payload"]["messages"] if message["role"] == "tool"]
    assert len(tools) == 1
    operation = json.loads(tools[0]["content"])
    assert operation["arguments"] == pending["required_confirmations"][0]["arguments"]
    memories = _memories(boundary)
    assert len(memories) == 1
    assert memories[0].content == boundary["arguments"]["content"]
    return completed


@pytest.mark.parametrize(
    ("setting", "changed"),
    [("LLM_MODEL", "different-approval-brain"), ("LLM_BASE_URL", "https://changed-approval.invalid/v1"), ("LLM_API_KEY", "local-rotated-key")],
    ids=["default-model", "default-endpoint", "default-key"],
)
def test_changed_default_address_cannot_execute_a_pending_approval(client, continuation_boundary, monkeypatch, setting, changed):
    boundary = continuation_boundary
    pending = _propose(client, boundary)
    monkeypatch.setenv(setting, changed)
    reload_settings()
    _assert_rejected_without_effect(client, boundary, pending)
    monkeypatch.setenv(setting, boundary["original"][setting])
    reload_settings()
    _assert_original_approved(client, boundary, pending)


@pytest.mark.parametrize("field", ["base_url", "api_key"], ids=["account-endpoint", "account-key"])
def test_changed_account_connection_cannot_execute_a_pending_approval(client, continuation_boundary, field):
    boundary = continuation_boundary
    original = {"model_name": "account-approval-brain", "base_url": "https://account-approval.invalid/v1", "api_key": "local-account-original-key"}
    assert client.post("/api/v1/llm/models", json=original).status_code == 200
    pending = _propose(client, boundary, model_routes={"planner": original["model_name"]})
    assert pending["execution_identity"]["model"] == original["model_name"]
    changed = {**original, field: "https://changed-account.invalid/v1" if field == "base_url" else "local-account-rotated-key"}
    assert client.post("/api/v1/llm/models", json=changed).status_code == 200
    _assert_rejected_without_effect(client, boundary, pending)
    assert client.post("/api/v1/llm/models", json=original).status_code == 200
    _assert_original_approved(client, boundary, pending)


def test_get_history_keeps_actual_execution_when_future_settings_change(client, continuation_boundary, monkeypatch):
    boundary = continuation_boundary
    pending = _propose(client, boundary)
    completed = _assert_original_approved(client, boundary, pending)
    for setting, value in {"LLM_MODEL": "future-model", "LLM_BASE_URL": "https://future.invalid/v1", "LLM_API_KEY": "future-local-key"}.items():
        monkeypatch.setenv(setting, value)
    reload_settings()

    direct = client.get(f"/api/v1/agent/runs/{completed['run_id']}").json()
    runs = client.get("/api/v1/agent/runs", params={"conversation_id": boundary["conversation"]["conversation_id"]}).json()["items"]
    listed = next(run for run in runs if run["run_id"] == completed["run_id"])
    history = client.get("/api/v1/chat/history", params={"conversation_id": boundary["conversation"]["conversation_id"]}).json()["items"]
    recorded = next(item["response_payload"] for item in history if item["response_payload"].get("run_id") == completed["run_id"])
    for observed in (direct, listed, recorded):
        for key in ("execution_identity", "context_snapshot", "model", "model_routes"):
            assert observed[key] == completed[key]
        assert "future-model" not in json.dumps(observed)
    assert len(boundary["calls"]) == boundary["proposal_call_count"] + 1
    assert len(_memories(boundary)) == 1


@pytest.mark.parametrize("account_route", [False, True], ids=["default-route", "account-route"])
def test_run_records_hide_endpoint_credentials_but_keep_real_private_route_in_memory(client, continuation_boundary, monkeypatch, account_route):
    boundary = continuation_boundary
    endpoint = "https://private-user:private-password@privacy-approval.invalid/v1?access_token=private-query#private-fragment"
    private_key = "local-private-credential-full-value"
    request = {}
    if account_route:
        assert client.post("/api/v1/llm/models", json={"model_name": "private-account-model", "base_url": endpoint, "api_key": private_key}).status_code == 200
        request["model"] = "private-account-model"
    else:
        monkeypatch.setenv("LLM_BASE_URL", endpoint)
        monkeypatch.setenv("LLM_API_KEY", private_key)
        reload_settings()
    pending = _propose(client, boundary, **request)
    proposal = next(call for call in boundary["calls"] if call["kind"] == "stream")
    assert proposal["base_url"] == endpoint
    assert proposal["api_key"] == private_key
    assert pending["execution_identity"]["endpoint"] == "https://privacy-approval.invalid/v1"
    completed = _assert_original_approved(client, boundary, pending)
    history = client.get("/api/v1/chat/history", params={"conversation_id": boundary["conversation"]["conversation_id"]}).json()["items"]
    recorded = next(item["response_payload"] for item in history if item["response_payload"].get("run_id") == completed["run_id"])
    for observed in (pending, completed, recorded):
        serialized = json.dumps(observed)
        for private in ("private-user", "private-password", "private-query", "private-fragment", private_key):
            assert private not in serialized
