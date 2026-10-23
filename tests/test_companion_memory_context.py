"""The memory notebook must change what the next real agent graph sends to its LLM.

Only the model boundary is replaced. Routes, authorization, storage, embeddings,
retrieval, prompt assembly, queued execution and tool handlers stay real.
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.api import deps
from app.core.config import reload_settings
from app.db.session import SessionLocal
from app.models.memory_card import MemoryCard
from app.models.memory_card_embedding import MemoryCardEmbedding
from app.services.llm_service import LLMAnswer, LLMCompletionResult, LLMService, LLMStreamChunk
from app.services.memory_markdown_store import MemoryMarkdownStore
from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


_NOTEBOOK = "/api/v1/companion/memory"


@pytest.fixture
def memory_boundary(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_MARKDOWN_ENABLED", "true")
    monkeypatch.setenv("MEMORY_ROOT_DIR", str(tmp_path / "notebook"))
    monkeypatch.setenv("MEMORY_CONSOLIDATION_MIN_TURNS", "1")
    reload_settings()
    store = MemoryMarkdownStore(root_dir=str(tmp_path / "notebook"))
    monkeypatch.setattr(deps, "_memory_store_singleton", store)
    # Reuse the registered observer instead of registering a second singleton.
    consolidation = deps._get_consolidation_service()
    monkeypatch.setattr(consolidation, "_store", store)
    calls = []

    def capture_answer(self, **kwargs):
        calls.append(("retrieval", copy.deepcopy(kwargs)))
        return LLMAnswer(answer="已收到上下文。")

    def capture_complete(self, **kwargs):
        calls.append(("complete", copy.deepcopy(kwargs)))
        return LLMCompletionResult(assistant_text="收到。")

    def capture_stream(self, **kwargs):
        calls.append(("stream", copy.deepcopy(kwargs)))
        yield LLMStreamChunk(delta_text="收到。")

    monkeypatch.setattr(LLMService, "answer_chat", capture_answer)
    monkeypatch.setattr(LLMService, "complete_chat", capture_complete)
    monkeypatch.setattr(LLMService, "stream_chat", capture_stream)
    team_id, user_id = register_workspace_user(
        client, prefix=f"notebook_context_{uuid4().hex[:6]}", display_name="Notebook owner",
    )
    conversation = _conversation(client)
    return {"store": store, "calls": calls, "team_id": team_id, "user_id": user_id, "conversation": conversation}


def _conversation(client, space_id=None):
    payload = {"title": "Notebook context"}
    if space_id is not None:
        payload["space_id"] = space_id
    response = client.post("/api/v1/conversations", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _notebook(client, conversation):
    response = client.get(_NOTEBOOK, params={"conversation_id": conversation["conversation_id"]})
    assert response.status_code == 200, response.text
    return response.json()


def _write_document(client, conversation, kind, content):
    current = next(item for item in _notebook(client, conversation)["documents"] if item["kind"] == kind)
    response = client.put(f"{_NOTEBOOK}/documents/{kind}", json={
        "conversation_id": conversation["conversation_id"], "content": content,
        "expected_revision": current["revision"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _create_card(client, conversation, marker):
    response = client.post(f"{_NOTEBOOK}/cards", json={
        "conversation_id": conversation["conversation_id"], "title": f"Preference {marker}",
        "content": f"User preference {marker}", "category": "preference",
    })
    assert response.status_code == 201, response.text
    return response.json()


def _patch_card(client, conversation, card, **changes):
    response = client.patch(f"{_NOTEBOOK}/cards/{card['memory_id']}", json={
        "conversation_id": conversation["conversation_id"], **changes,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _chat(client, boundary, conversation, *, include_memory=True, task="请回答我的偏好。"):
    calls = boundary["calls"]
    start_index = len(calls)
    queued = client.post("/api/v1/companion/runs", json={
        "conversation_id": conversation["conversation_id"], "task": task,
        "mode": "chat", "backend": "api", "include_memory": include_memory,
    })
    assert queued.status_code == 201, queued.text
    stream = client.get(f"/api/v1{queued.json()['stream_url']}")
    assert stream.status_code == 200, stream.text
    run = client.get(f"/api/v1/agent/runs/{queued.json()['run_id']}")
    assert run.status_code == 200
    assert run.json()["status"] == "completed", run.text
    observed = calls[start_index:]
    assert any(kind == "retrieval" for kind, _ in observed)
    assert any(kind == "stream" for kind, _ in observed)
    return observed


def _input_text(calls):
    # Examine every outgoing prompt, not the fake response or the HTTP listing.
    return json.dumps([
        {key: value for key, value in kwargs.items() if key in {"message", "messages", "conversation_messages", "system_prompt"}}
        for _, kwargs in calls
    ], ensure_ascii=False)


def _recall(boundary, *, query, space_id=None, team_id=None, user_id=None):
    arguments = {"query": query, "limit": 20}
    if space_id is not None:
        arguments["space_id"] = space_id
    with SessionLocal() as db:
        service = deps.create_background_agent_service(db)
        return service.tool_service.execute(
            team_id=team_id or boundary["team_id"], user_id=user_id or boundary["user_id"],
            action="recall_memory", arguments=arguments,
        )["memories"]


def test_notebook_document_edits_reach_next_prompt_and_same_space_new_chat(client, memory_boundary):
    boundary = memory_boundary
    original = boundary["conversation"]
    markers = {
        "self_model": "SELF_NOTEBOOK_UNIQUE_458", "long_term": "LONG_NOTEBOOK_UNIQUE_458",
        "recent_context": "RECENT_NOTEBOOK_UNIQUE_458",
    }
    for kind, marker in markers.items():
        _write_document(client, original, kind, marker)

    fresh = _conversation(client, original["space_id"])
    assert fresh["conversation_id"] != original["conversation_id"]
    assert fresh["space_id"] == original["space_id"]
    observed = _chat(client, boundary, fresh)
    for kind, kwargs in observed:
        if kind in {"retrieval", "stream"}:
            system = kwargs["system_prompt"] if kind == "retrieval" else kwargs["messages"][0]["content"]
            assert all(marker in system for marker in markers.values())

    _write_document(client, original, "long_term", "UPDATED_NOTEBOOK_UNIQUE_458")
    updated = _input_text(_chat(client, boundary, _conversation(client, original["space_id"])))
    assert "UPDATED_NOTEBOOK_UNIQUE_458" in updated
    assert markers["long_term"] not in updated

    _write_document(client, original, "long_term", "")
    cleared = _input_text(_chat(client, boundary, _conversation(client, original["space_id"])))
    assert "UPDATED_NOTEBOOK_UNIQUE_458" not in cleared
    assert markers["long_term"] not in cleared


def test_memory_off_suppresses_all_notebook_inputs_without_erasing_them(client, memory_boundary):
    boundary = memory_boundary
    conversation = boundary["conversation"]
    markers = []
    for kind in ("self_model", "long_term", "recent_context"):
        marker = f"PRIVATE_OFF_{kind}_782"
        markers.append(marker)
        _write_document(client, conversation, kind, marker)
    card_marker = "PRIVATE_OFF_CARD_782"
    card = _create_card(client, conversation, card_marker)
    markers.append(card_marker)

    fresh = _conversation(client, conversation["space_id"])
    observed = _chat(client, boundary, fresh, include_memory=False)
    assert all(marker not in _input_text(observed) for marker in markers)
    assert all(not kwargs.get("tools") for kind, kwargs in observed if kind == "stream")
    stored = _notebook(client, conversation)
    assert card["memory_id"] in {item["memory_id"] for item in stored["items"]}
    assert all(marker in json.dumps(stored, ensure_ascii=False) for marker in markers)

    enabled = _input_text(_chat(client, boundary, _conversation(client, conversation["space_id"])))
    assert all(marker in enabled for marker in markers)


def test_card_edit_pause_resume_and_delete_change_semantic_input_and_real_recall(client, memory_boundary):
    boundary = memory_boundary
    conversation = boundary["conversation"]
    card = _create_card(client, conversation, "CARD_OLD_964")
    with SessionLocal() as db:
        # Imported/older cards can have summaries that the notebook cannot edit.
        db.get(MemoryCard, card["memory_id"]).summary = "User preference CARD_OLD_964"
        db.commit()

    def model_input():
        return _input_text(_chat(client, boundary, _conversation(client, conversation["space_id"])))

    assert "CARD_OLD_964" in model_input()
    assert [item["memory_id"] for item in _recall(boundary, query="CARD_OLD_964")] == [card["memory_id"]]
    _patch_card(client, conversation, card, content="User preference CARD_NEW_964", title="CARD_NEW_964")
    edited = model_input()
    assert "CARD_NEW_964" in edited
    assert "CARD_OLD_964" not in edited
    assert _recall(boundary, query="CARD_OLD_964") == []
    assert "CARD_OLD_964" not in json.dumps(_recall(boundary, query="CARD_NEW_964"))

    _patch_card(client, conversation, card, status="disabled")
    assert "CARD_NEW_964" not in model_input()
    assert _recall(boundary, query="CARD_NEW_964") == []

    _patch_card(client, conversation, card, status="active")
    assert "CARD_NEW_964" in model_input()
    assert [item["memory_id"] for item in _recall(boundary, query="CARD_NEW_964")] == [card["memory_id"]]

    deleted = client.delete(f"{_NOTEBOOK}/cards/{card['memory_id']}", params={"conversation_id": conversation["conversation_id"]})
    assert deleted.status_code == 204, deleted.text
    assert "CARD_NEW_964" not in model_input()
    assert _recall(boundary, query="CARD_NEW_964") == []


def test_model_approved_memory_also_reaches_later_chat_without_tools(client, memory_boundary, monkeypatch):
    boundary = memory_boundary
    conversation = boundary["conversation"]
    original_stream = LLMService.stream_chat
    proposed = False

    def propose_memory(self, **kwargs):
        nonlocal proposed
        if proposed:
            yield from original_stream(self, **kwargs)
            return
        proposed = True
        boundary["calls"].append(("stream", copy.deepcopy(kwargs)))
        yield LLMStreamChunk(tool_call_deltas=[{
            "index": 0, "id": "remember-approved-preference", "type": "function",
            "function": {"name": "create_memory_card", "arguments": json.dumps({
                "title": "Approved preference", "content": "APPROVED_MEMORY_184",
            })},
        }])

    monkeypatch.setattr(LLMService, "stream_chat", propose_memory)
    queued = client.post("/api/v1/companion/runs", json={
        "conversation_id": conversation["conversation_id"], "task": "记住我刚才的偏好。", "mode": "agent",
    })
    assert queued.status_code == 201, queued.text
    run_id = queued.json()["run_id"]
    assert client.get(f"/api/v1{queued.json()['stream_url']}").status_code == 200
    pending = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert pending["status"] == "requires_confirmation"
    assert _notebook(client, conversation)["items"] == []
    approved = client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={"approval_token": pending["approval_token"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "completed"
    assert "APPROVED_MEMORY_184" in json.dumps(_notebook(client, conversation))

    # Use a new conversation so old chat text cannot masquerade as retrieval.
    fresh = _conversation(client, conversation["space_id"])
    assert "APPROVED_MEMORY_184" in _input_text(_chat(client, boundary, fresh))


@pytest.mark.parametrize("offset_hours", [0, 8], ids=["utc", "offset"])
def test_legacy_expiration_is_respected_by_prompt_and_scoped_or_unscoped_recall(client, memory_boundary, offset_hours):
    boundary = memory_boundary
    conversation = boundary["conversation"]
    expired = _create_card(client, conversation, "EXPIRY_OLD_613")
    future = _create_card(client, conversation, "EXPIRY_FUTURE_613")
    expired_status = _create_card(client, conversation, "EXPIRY_STATUS_613")
    now = datetime.now(timezone(timedelta(hours=offset_hours)))
    with SessionLocal() as db:
        # Existing cards can carry timestamps created through the legacy API.
        # SQLite returns these naive; PostgreSQL preserves timezone awareness.
        db.get(MemoryCard, expired["memory_id"]).expires_at = now - timedelta(days=1)
        db.get(MemoryCard, future["memory_id"]).expires_at = now + timedelta(days=1)
        db.get(MemoryCard, expired_status["memory_id"]).status = "expired"
        db.commit()

    states = {item["memory_id"]: item["effective_status"] for item in _notebook(client, conversation)["items"]}
    assert states[expired["memory_id"]] == "expired"
    assert states[future["memory_id"]] == "active"
    observed = _input_text(_chat(client, boundary, _conversation(client, conversation["space_id"])))
    assert "EXPIRY_FUTURE_613" in observed
    assert "EXPIRY_OLD_613" not in observed
    assert "EXPIRY_STATUS_613" not in observed
    for space_id in (None, conversation["space_id"]):
        recalled = _recall(boundary, query="EXPIRY_", space_id=space_id)
        assert [item["memory_id"] for item in recalled] == [future["memory_id"]]


def test_notebook_context_never_crosses_accounts_in_the_same_team(client, memory_boundary):
    boundary = memory_boundary
    original = boundary["conversation"]
    first_marker = "FIRST_PRIVATE_NOTEBOOK_827"
    first_card = _create_card(client, original, first_marker)
    _write_document(client, original, "long_term", first_marker)
    _, second_user = register_workspace_user_in_team(
        client, team_id=boundary["team_id"], prefix=f"other_notebook_{uuid4().hex[:6]}", display_name="Other friend",
    )
    second = _conversation(client)
    second_marker = "SECOND_PRIVATE_NOTEBOOK_827"
    second_card = _create_card(client, second, second_marker)
    _write_document(client, second, "long_term", second_marker)

    observed = _input_text(_chat(client, boundary, second))
    assert second_marker in observed
    assert first_marker not in observed
    assert _recall(boundary, query=first_marker, user_id=second_user) == []
    assert _recall(boundary, query=second_marker) == []
    assert [item["memory_id"] for item in _recall(boundary, query=second_marker, user_id=second_user)] == [second_card["memory_id"]]

    assert client.get(_NOTEBOOK, params={"conversation_id": original["conversation_id"]}).status_code == 404
    assert client.patch(f"{_NOTEBOOK}/cards/{first_card['memory_id']}", json={
        "conversation_id": second["conversation_id"], "content": "attempted overwrite",
    }).status_code == 404
    assert client.delete(f"{_NOTEBOOK}/cards/{first_card['memory_id']}", params={
        "conversation_id": second["conversation_id"],
    }).status_code == 404


def test_another_space_gets_personal_global_cards_but_not_space_notes(client, memory_boundary):
    boundary = memory_boundary
    original = boundary["conversation"]
    _write_document(client, original, "long_term", "SPACE_ONLY_NOTE_328")
    local = _create_card(client, original, "SPACE_ONLY_CARD_328")
    global_card = _create_card(client, original, "PERSONAL_GLOBAL_328")
    with SessionLocal() as db:
        # The existing memory layer supports imported personal global cards.
        row = db.get(MemoryCard, global_card["memory_id"])
        row.scope_level = "global"
        row.space_id = None
        embedding = db.query(MemoryCardEmbedding).filter_by(memory_id=row.memory_id).one()
        embedding.space_id = None
        db.commit()

    created = client.post("/api/v1/spaces", json={"name": "Separate notebook"})
    assert created.status_code == 201, created.text
    other = _conversation(client, created.json()["space_id"])
    observed = _input_text(_chat(client, boundary, other))
    assert "PERSONAL_GLOBAL_328" in observed
    assert "SPACE_ONLY_CARD_328" not in observed
    assert "SPACE_ONLY_NOTE_328" not in observed
    listed = {item["memory_id"] for item in _notebook(client, other)["items"]}
    assert global_card["memory_id"] in listed
    assert local["memory_id"] not in listed
    recalled = _recall(boundary, query="328", space_id=other["space_id"])
    assert [item["memory_id"] for item in recalled] == [global_card["memory_id"]]


def test_companion_turns_do_not_automatically_repopulate_manual_notebook(client, memory_boundary):
    boundary = memory_boundary
    conversation = boundary["conversation"]
    _write_document(client, conversation, "self_model", "SELF_MANUAL_ONLY_527")
    _write_document(client, conversation, "long_term", "LONG_MANUAL_ONLY_527")
    _write_document(client, conversation, "recent_context", "RECENT_MANUAL_ONLY_527")
    directory = boundary["store"].ensure_workspace(boundary["team_id"], boundary["user_id"], conversation["space_id"])

    def snapshot():
        return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}

    before = snapshot()
    for enabled in (True, False):
        observed = _chat(client, boundary, conversation, include_memory=enabled, task="我喜欢AUTOWRITE_FORBIDDEN_527。")
        assert {kind for kind, _ in observed} == {"retrieval", "stream"}
        assert snapshot() == before


def test_memory_off_keeps_explicit_current_conversation_history(client, memory_boundary):
    boundary = memory_boundary
    conversation = boundary["conversation"]
    _write_document(client, conversation, "long_term", "NOTEBOOK_OFF_ONLY_429")
    _chat(client, boundary, conversation, include_memory=False, task="当前对话暗号是HISTORY_ALLOWED_429。")
    observed = _input_text(_chat(client, boundary, conversation, include_memory=False, task="继续刚才的话题。"))
    assert "HISTORY_ALLOWED_429" in observed
    assert "NOTEBOOK_OFF_ONLY_429" not in observed
