"""A companion approval authorizes the operation the user actually reviewed."""

import json
from uuid import uuid4

import pytest

from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.memory_card import MemoryCard
from app.services.llm_service import LLMCompletionResult, LLMService, LLMStreamChunk
from tests.auth_helpers import register_workspace_user


def test_each_companion_memory_write_requires_its_own_approval(client, monkeypatch):
    team_id, user_id = register_workspace_user(
        client, prefix=f"pet_approval_{uuid4().hex[:6]}", display_name="Companion Friend",
    )
    conversation = client.post("/api/v1/conversations", json={"title": "Memory approvals"}).json()
    queued = client.post("/api/v1/companion/runs", json={
        "conversation_id": conversation["conversation_id"], "task": "记住我喜欢薄荷绿。",
    })
    assert queued.status_code == 201
    run_id = queued.json()["run_id"]
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        space_id = run.space_id
        first_arguments = {"space_id": space_id, "title": "第一条", "content": "我喜欢薄荷绿。"}
        run.status = "requires_confirmation"
        run.required_confirmations_json = json.dumps([{
            "tool_name": "create_memory_card", "arguments": first_arguments,
            "dangerous": True, "requires_confirmation": True,
        }])
        run.loop_state_json = json.dumps({
            "messages": [
                {"role": "user", "content": run.task},
                {"role": "assistant", "tool_calls": [{
                    "id": "first-write", "type": "function",
                    "function": {"name": "create_memory_card", "arguments": json.dumps(first_arguments)},
                }]},
            ],
            "pending_tool_call_id": "first-write",
        })
        db.commit()

    second_arguments = {"space_id": space_id, "title": "第二条", "content": "这是另一条需要独立同意的记忆。"}
    model_calls = []

    def propose_another_write(self, **kwargs):
        model_calls.append(kwargs)
        if len(model_calls) == 1:
            return LLMCompletionResult(assistant_text="还有一条需要你确认。", tool_calls=[{
                "id": "second-write", "type": "function",
                "function": {"name": "create_memory_card", "arguments": json.dumps(second_arguments)},
            }])
        return LLMCompletionResult(assistant_text="两条都记下了。")

    monkeypatch.setattr(LLMService, "complete_chat", propose_another_write)

    first_confirmation = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={})
    assert first_confirmation.status_code == 200
    first_result = first_confirmation.json()
    assert first_result["status"] == "requires_confirmation"
    assert first_result["required_confirmations"][0]["arguments"]["title"] == "第二条"
    assert len(model_calls) == 1
    with SessionLocal() as db:
        memories = db.query(MemoryCard).filter_by(team_id=team_id, user_id=user_id).all()
        assert [memory.title for memory in memories] == ["第一条"]
        pending_run = db.get(AgentRun, run_id)
        assert pending_run.status == "requires_confirmation"
        assert json.loads(pending_run.loop_state_json)["pending_tool_call_id"] == "second-write"

    second_confirmation = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={})
    assert second_confirmation.status_code == 200
    assert second_confirmation.json()["status"] == "completed"
    assert second_confirmation.json()["required_confirmations"] == []
    with SessionLocal() as db:
        memories = db.query(MemoryCard).filter_by(team_id=team_id, user_id=user_id).all()
        assert {memory.title for memory in memories} == {"第一条", "第二条"}
        assert db.get(AgentRun, run_id).loop_state_json is None

    # A stale or repeated click cannot perform either write again.
    assert client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={}).status_code == 400
    with SessionLocal() as db:
        assert db.query(MemoryCard).filter_by(team_id=team_id, user_id=user_id).count() == 2


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_memory_write_uses_trusted_space_when_model_only_supplies_content(client, monkeypatch, streaming):
    team_id, user_id = register_workspace_user(
        client, prefix=f"pet_remember_{uuid4().hex[:6]}", display_name="Companion Friend",
    )
    conversation = client.post("/api/v1/conversations", json={"title": "Remember my preference"}).json()
    arguments = {"title": "喜欢的颜色", "content": "我喜欢薄荷绿。"}
    calls = []

    def complete(self, **kwargs):
        calls.append(kwargs)
        if not streaming and len(calls) == 1:
            return LLMCompletionResult(assistant_text="", tool_calls=[{
                "id": "remember-color", "type": "function",
                "function": {"name": "create_memory_card", "arguments": json.dumps(arguments)},
            }])
        return LLMCompletionResult(assistant_text="记下了。")

    def stream(self, **kwargs):
        yield LLMStreamChunk(tool_call_deltas=[{
            "index": 0,
            "function": {"name": "create_memory_card", "arguments": json.dumps(arguments)},
        }])

    monkeypatch.setattr(LLMService, "complete_chat", complete)
    monkeypatch.setattr(LLMService, "stream_chat", stream)
    if streaming:
        queued = client.post("/api/v1/companion/runs", json={
            "task": "记住我喜欢薄荷绿。", "conversation_id": conversation["conversation_id"],
        })
        assert queued.status_code == 201
        run_id = queued.json()["run_id"]
        streamed = client.get(f"/api/v1{queued.json()['stream_url']}")
        assert streamed.status_code == 200
        assert "confirmation.required" in streamed.text
    else:
        response = client.post("/api/v1/agent/run", json={
            "task": "记住我喜欢薄荷绿。", "conversation_id": conversation["conversation_id"],
            "trigger_channel": "companion", "enabled_tools": ["create_memory_card"],
            "include_library": False, "include_memory": False,
        })
        assert response.status_code == 200
        run_id = response.json()["run_id"]

    run = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert run["status"] == "requires_confirmation"
    pending = run["required_confirmations"][0]
    assert pending["arguments"]["space_id"] == conversation["space_id"]
    assert pending["arguments"]["content"] == arguments["content"]
    with SessionLocal() as db:
        assert db.query(MemoryCard).filter_by(team_id=team_id, user_id=user_id).count() == 0

    confirmed = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "completed"
    with SessionLocal() as db:
        memories = db.query(MemoryCard).filter_by(team_id=team_id, user_id=user_id).all()
        assert len(memories) == 1
        assert memories[0].space_id == conversation["space_id"]
        assert memories[0].content == arguments["content"]
