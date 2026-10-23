"""Protect the companion entry point and its non-replayable history records."""

from uuid import uuid4

import pytest

from app.db.session import SessionLocal
from app.models.chat_history import ChatHistory
from app.services.rag_chat_service import RagChatService
from tests.auth_helpers import register_workspace_user


def test_companion_entry_and_assets_are_available_without_login(client):
    page = client.get("/companion")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert 'id="pet"' in page.text
    assert 'src="/web/companion.js?v=0280"' in page.text
    assert client.head("/companion").status_code == 200
    for asset in ("companion.js", "companion-state.mjs", "companion.css"):
        response = client.get(f"/web/{asset}")
        assert response.status_code == 200
        assert response.content


@pytest.mark.parametrize("channel", ["companion", "companion_event"])
def test_generic_history_edit_cannot_replay_companion_records(client, monkeypatch, channel):
    register_workspace_user(
        client, prefix=f"pet_edit_{uuid4().hex[:6]}", display_name="Companion Friend",
    )
    if channel == "companion_event":
        event = client.post("/api/v1/companion/events", json={"kind": "pet"})
        assert event.status_code == 201
        message_id = event.json()["event_id"]
    else:
        conversation = client.post("/api/v1/conversations", json={"title": "Companion edit"}).json()
        queued = client.post("/api/v1/companion/runs", json={
            "task": "你好。", "mode": "chat", "include_memory": False,
            "conversation_id": conversation["conversation_id"],
        })
        assert queued.status_code == 201
        stream = client.get(f"/api/v1{queued.json()['stream_url']}")
        assert stream.status_code == 200
        history = client.get("/api/v1/chat/history", params={
            "conversation_id": conversation["conversation_id"],
        }).json()
        message_id = history["items"][0]["message_id"]

    with SessionLocal() as db:
        original = db.get(ChatHistory, message_id)
        before = (original.request_text, original.response_text, original.channel)

    def must_not_regenerate(*args, **kwargs):
        pytest.fail("Editing companion history must not call a different chat runtime")

    monkeypatch.setattr(RagChatService, "ask", must_not_regenerate)
    edited = client.put(f"/api/v1/chat/history/{message_id}", json={"request_text": "替换成别的话。"})
    assert edited.status_code == 400
    with SessionLocal() as db:
        unchanged = db.get(ChatHistory, message_id)
        assert (unchanged.request_text, unchanged.response_text, unchanged.channel) == before
