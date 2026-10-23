"""Approval retries must never authorize a newly proposed operation."""

import json
import re

import pytest
from sqlalchemy import select, update

from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.memory_card import MemoryCard
from app.services.agent_service import AgentService
from app.services.llm_service import LLMCompletionResult, LLMService
from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


def _waiting_run(client, *, companion=True):
    team_id, user_id = register_workspace_user(
        client, prefix="approval_identity", display_name="Approval owner",
    )
    conversation = client.post("/api/v1/conversations", json={"title": "Exact approval"}).json()
    response = client.post("/api/v1/agent/runs", json={
        "task": "记住我喜欢薄荷绿。", "conversation_id": conversation["conversation_id"],
        "trigger_channel": "companion" if companion else "agent",
        "include_memory": False, "include_library": False,
        "enabled_tools": ["create_memory_card"],
    })
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    arguments = {
        "space_id": conversation["space_id"],
        "title": "喜欢的颜色", "content": "我喜欢薄荷绿。", "category": "agent",
    }
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        run.status = "requires_confirmation"
        run.required_confirmations_json = json.dumps([{
            "tool_name": "create_memory_card", "arguments": arguments,
            "dangerous": True, "requires_confirmation": True,
        }], ensure_ascii=False)
        run.loop_state_json = json.dumps({
            "messages": [
                {"role": "user", "content": run.task},
                {"role": "assistant", "tool_calls": [{
                    "id": "first-write", "type": "function",
                    "function": {"name": "create_memory_card", "arguments": json.dumps(arguments)},
                }]},
            ],
            "pending_tool_call_id": "first-write",
        }, ensure_ascii=False)
        db.commit()
    return team_id, user_id, run_id, arguments


def _memory_count(team_id, user_id):
    with SessionLocal() as db:
        return len(db.scalars(select(MemoryCard).where(
            MemoryCard.team_id == team_id, MemoryCard.user_id == user_id,
        )).all())


@pytest.mark.parametrize(
    "body", [{}, {"approval_token": "wrong-token"}, {"approval_token": "错误令牌"}],
    ids=["missing", "wrong", "unicode"],
)
def test_companion_approval_requires_the_displayed_operation_token(client, body):
    team_id, user_id, run_id, _ = _waiting_run(client)
    pending = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert re.fullmatch(r"[0-9a-f]{64}", pending["approval_token"])
    rejected = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json=body)
    assert rejected.status_code == 400
    unchanged = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert unchanged["status"] == "requires_confirmation"
    assert unchanged["approval_token"] == pending["approval_token"]
    assert _memory_count(team_id, user_id) == 0


def test_old_token_cannot_confirm_next_approval_with_identical_arguments(client, monkeypatch):
    team_id, user_id, run_id, arguments = _waiting_run(client)
    token_a = client.get(f"/api/v1/agent/runs/{run_id}").json()["approval_token"]
    model_calls = []

    def propose_next_write(self, **kwargs):
        model_calls.append(kwargs)
        if len(model_calls) == 1:
            return LLMCompletionResult(assistant_text="还有一条，请再确认。", tool_calls=[{
                "id": "second-write", "type": "function",
                "function": {"name": "create_memory_card", "arguments": json.dumps(arguments)},
            }])
        return LLMCompletionResult(assistant_text="两次操作都已经返回。")

    monkeypatch.setattr(LLMService, "complete_chat", propose_next_write)
    first = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={"approval_token": token_a})
    assert first.status_code == 200
    assert first.json()["status"] == "requires_confirmation"
    token_b = first.json()["approval_token"]
    assert re.fullmatch(r"[0-9a-f]{64}", token_b)
    assert token_b != token_a
    assert first.json()["required_confirmations"][0]["arguments"] == arguments
    assert _memory_count(team_id, user_id) == 1

    # A delayed click/retry must not authorize B, even when its arguments match A.
    stale = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={"approval_token": token_a})
    assert stale.status_code == 400
    assert _memory_count(team_id, user_id) == 1
    assert len(model_calls) == 1
    pending_b = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert pending_b["status"] == "requires_confirmation"
    assert pending_b["approval_token"] == token_b

    second = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={"approval_token": token_b})
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert second.json()["required_confirmations"] == []
    assert second.json()["approval_token"] is None
    assert _memory_count(team_id, user_id) == 2
    assert len(model_calls) == 2


def test_approval_token_does_not_grant_another_user_access(client):
    team_id, user_id, run_id, _ = _waiting_run(client)
    token = client.get(f"/api/v1/agent/runs/{run_id}").json()["approval_token"]
    register_workspace_user_in_team(
        client, team_id=team_id, prefix="approval_foreign", display_name="Other user",
    )
    assert client.get(f"/api/v1/agent/runs/{run_id}").status_code == 404
    rejected = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={"approval_token": token})
    assert rejected.status_code == 404
    assert _memory_count(team_id, user_id) == 0
    client.cookies.clear()
    assert client.post(
        f"/api/v1/agent/runs/{run_id}/confirm", json={"approval_token": token},
    ).status_code == 401


def test_approval_changed_after_validation_cannot_be_claimed(client, monkeypatch):
    team_id, user_id, run_id, arguments = _waiting_run(client)
    token_a = client.get(f"/api/v1/agent/runs/{run_id}").json()["approval_token"]
    load_loop_state = AgentService._load_loop_state
    replacement_arguments = {**arguments, "title": "A different approval"}
    swapped = []

    def replace_after_read(self, run):
        original = load_loop_state(self, run)
        if run.run_id == run_id and not swapped:
            # Another request changes the pending operation between validation
            # and claim. The original request's identity map still contains A.
            swapped.append(True)
            replacement_state = {**original, "pending_tool_call_id": "replacement-write"}
            with SessionLocal() as db:
                db.execute(update(AgentRun).where(AgentRun.run_id == run_id).values(
                    required_confirmations_json=json.dumps([{
                        "tool_name": "create_memory_card", "arguments": replacement_arguments,
                        "dangerous": True, "requires_confirmation": True,
                    }]),
                    loop_state_json=json.dumps(replacement_state),
                ))
                db.commit()
        return original

    monkeypatch.setattr(AgentService, "_load_loop_state", replace_after_read)
    rejected = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={"approval_token": token_a})
    assert rejected.status_code == 400
    assert swapped == [True]
    assert _memory_count(team_id, user_id) == 0
    pending_b = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert pending_b["status"] == "requires_confirmation"
    assert pending_b["approval_token"] != token_a
    assert pending_b["required_confirmations"][0]["arguments"] == replacement_arguments


def test_noncompanion_legacy_confirmation_still_accepts_missing_token(client, monkeypatch):
    team_id, user_id, run_id, _ = _waiting_run(client, companion=False)
    monkeypatch.setattr(
        LLMService, "complete_chat", lambda self, **kwargs: LLMCompletionResult(assistant_text="已记下。"),
    )
    approved = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={})
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    assert _memory_count(team_id, user_id) == 1
