import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.chat_history import ChatHistory
from tests.auth_helpers import (
    login_existing_user,
    register_workspace_user,
    register_workspace_user_in_team,
)


def _register(client):
    return register_workspace_user(client, prefix=f"pet_{uuid4().hex[:6]}", display_name="彩宝的朋友")


def test_companion_requires_authenticated_active_user(client):
    for method, path, payload in (
        ("get", "/api/v1/companion/profile", None),
        ("post", "/api/v1/companion/runs", {"task": "你好"}),
        ("post", "/api/v1/companion/events", {"kind": "pet"}),
    ):
        response = getattr(client, method)(path, **({"json": payload} if payload else {}))
        assert response.status_code == 401


def test_companion_profile_exposes_honest_demo_runtime_and_expressions(client):
    _register(client)
    response = client.get("/api/v1/companion/profile")
    assert response.status_code == 200
    profile = response.json()
    assert profile["name"] == "彩宝"
    assert profile["species"] == "cat"
    assert profile["state"]["activity"] == "idle"
    assert profile["runtime"]["demo_mode"] is True
    assert profile["runtime"]["provider"] == "mock"
    assert {item["id"] for item in profile["emotions"]} == {
        "idle", "happy", "curious", "focused", "sleepy", "concerned",
    }
    assert {item["id"] for item in profile["capabilities"]} >= {"agent", "memory", "voice"}


def test_companion_events_persist_but_are_isolated_from_teammates(client):
    team_id, first_user = _register(client)
    response = client.post("/api/v1/companion/events", json={"kind": "rest"})
    assert response.status_code == 201
    assert response.json()["state"]["activity"] == "sleeping"
    event_id = response.json()["event_id"]
    with SessionLocal() as db:
        record = db.get(ChatHistory, event_id)
        assert record.user_id == first_user
        assert record.channel == "companion_event"
        assert record.conversation_id is None

    register_workspace_user_in_team(
        client, team_id=team_id, prefix="pet_teammate", display_name="Another friend",
    )
    assert client.get("/api/v1/companion/profile").json()["state"]["activity"] == "idle"
    login_existing_user(client, user_id=first_user)
    assert client.get("/api/v1/companion/profile").json()["state"]["activity"] == "sleeping"
    wake = client.post("/api/v1/companion/events", json={"kind": "wake"})
    assert wake.json()["state"]["activity"] == "idle"


@pytest.mark.parametrize("kind,emotion", [("pet", "happy"), ("wave", "curious"), ("feed", "happy")])
def test_companion_interactions_are_local_reactions_without_agent_runs(client, kind, emotion):
    team_id, user_id = _register(client)
    response = client.post("/api/v1/companion/events", json={"kind": kind})
    assert response.status_code == 201
    assert response.json()["state"]["emotion"] == emotion
    assert response.json()["message"]
    with SessionLocal() as db:
        assert db.query(AgentRun).filter_by(team_id=team_id, user_id=user_id).count() == 0


def test_companion_chat_queues_real_run_without_tools_and_streams_history(client):
    team_id, user_id = _register(client)
    conversation = client.post("/api/v1/conversations", json={"title": "和我的彩宝聊天"}).json()
    response = client.post("/api/v1/companion/runs", json={
        "task": "  今天想聊聊  ", "mode": "chat",
        "conversation_id": conversation["conversation_id"], "include_memory": False,
    })
    assert response.status_code == 201
    queued = response.json()
    assert queued["status"] == "queued"
    with SessionLocal() as db:
        run = db.get(AgentRun, queued["run_id"])
        request = json.loads(run.request_payload_json)
        assert (run.team_id, run.user_id, run.trigger_channel) == (team_id, user_id, "companion")
        assert run.task == "今天想聊聊"
        assert request["enabled_tools"] == []
        assert request["include_memory"] is False
        assert request["include_library"] is False
        assert request["confirm_dangerous_actions"] is False
        assert "彩宝" in request["system_prompt"]
        assert "不代表你有意识" in request["system_prompt"]

    state = client.get("/api/v1/companion/profile").json()["state"]
    assert state["activity"] == "thinking"
    # A touch should never claim the real task has stopped.
    touched = client.post("/api/v1/companion/events", json={"kind": "pet"}).json()
    assert touched["state"]["activity"] == "thinking"

    stream = client.get(f"/api/v1{queued['stream_url']}")
    assert stream.status_code == 200
    assert "event: run.completed" in stream.text
    run = client.get(f"/api/v1/agent/runs/{queued['run_id']}").json()
    assert run["status"] == "completed"
    assert run["trigger_channel"] == "companion"
    assert run["tool_calls"] == []
    history = client.get("/api/v1/chat/history", params={"conversation_id": conversation["conversation_id"]}).json()
    assert history["items"][0]["channel"] == "companion"
    assert client.get("/api/v1/companion/profile").json()["state"]["emotion"] == "happy"


def test_companion_agent_tools_and_memory_switch_are_server_controlled(client):
    _register(client)
    for include_memory in (True, False):
        response = client.post("/api/v1/companion/runs", json={
            "task": "帮我找点资料", "include_memory": include_memory,
        })
        assert response.status_code == 201
        with SessionLocal() as db:
            request = json.loads(db.get(AgentRun, response.json()["run_id"]).request_payload_json)
        assert "web_search" in request["enabled_tools"]
        assert "shell_exec" not in request["enabled_tools"]
        assert ("recall_memory" in request["enabled_tools"]) is include_memory
        assert ("create_memory_card" in request["enabled_tools"]) is include_memory
        assert request["max_steps"] == 6
        assert request["confirm_dangerous_actions"] is False


def test_companion_refuses_foreign_conversation_and_spoofed_policy(client):
    team_id, _ = _register(client)
    conversation_id = client.post("/api/v1/conversations", json={"title": "Private"}).json()["conversation_id"]
    register_workspace_user_in_team(client, team_id=team_id, prefix="pet_other", display_name="Other")
    response = client.post("/api/v1/companion/runs", json={"task": "hi", "conversation_id": conversation_id})
    assert response.status_code == 404
    for override in ({"user_id": "someone"}, {"enabled_tools": ["shell_exec"]}, {"confirm_dangerous_actions": True}):
        assert client.post("/api/v1/companion/runs", json={"task": "hi", **override}).status_code == 422


def test_companion_rejects_empty_tasks_and_unknown_interactions(client):
    _register(client)
    for task in ("", "   ", "x" * 4001):
        assert client.post("/api/v1/companion/runs", json={"task": task}).status_code == 422
    assert client.post("/api/v1/companion/events", json={"kind": "delete"}).status_code == 422


def test_companion_reaction_decays_without_invented_needs(client):
    _register(client)
    response = client.post("/api/v1/companion/events", json={"kind": "pet"}).json()
    with SessionLocal() as db:
        event = db.get(ChatHistory, response["event_id"])
        event.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()
    profile = client.get("/api/v1/companion/profile").json()
    assert profile["state"]["emotion"] == "idle"
    assert profile["state"]["activity"] == "idle"
