"""The companion notebook edits the actual memories, without asking an LLM."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
import httpx

from app.api import deps
from app.core.config import reload_settings
from app.db.session import SessionLocal
from app.models.chat_history import ChatHistory
from app.models.memory_card import MemoryCard
from app.models.memory_card_embedding import MemoryCardEmbedding
from app.services.llm_service import LLMService
from app.services.embedding_service import EmbeddingService
from app.services.memory_markdown_store import MemoryDocumentConflictError, MemoryMarkdownStore
from tests.auth_helpers import login_existing_user, register_workspace_user, register_workspace_user_in_team


BASE = "/api/v1/companion/memory"


@pytest.fixture(autouse=True)
def isolated_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_MARKDOWN_ENABLED", "true")
    monkeypatch.setenv("MEMORY_ROOT_DIR", str(tmp_path / "memory"))
    reload_settings()
    store = MemoryMarkdownStore(str(tmp_path / "memory"))
    monkeypatch.setattr(deps, "_memory_store_singleton", store)
    return store


def _account(client):
    team, user = register_workspace_user(client, prefix="notebook", display_name="Notebook friend")
    conversation = client.post("/api/v1/conversations", json={"title": "日常"}).json()
    return team, user, conversation


def _create(client, conversation, **changes):
    payload = {"conversation_id": conversation["conversation_id"], "title": "回复偏好", "content": "我喜欢简短回复"}
    payload.update(changes)
    response = client.post(BASE + "/cards", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _list(client, conversation, **params):
    response = client.get(BASE, params={"conversation_id": conversation["conversation_id"], **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_notebook_requires_authentication_and_cannot_accept_identity(client):
    assert client.get(BASE, params={"conversation_id": str(uuid4())}).status_code == 401
    _, _, conversation = _account(client)
    response = client.post(BASE + "/cards", json={
        "conversation_id": conversation["conversation_id"], "title": "标题", "content": "内容", "user_id": "other",
    })
    assert response.status_code == 422
    assert client.get(BASE, params={"conversation_id": conversation["conversation_id"], "limit": 101}).status_code == 422


def test_explicit_crud_and_documents_work_without_generation_model(client, monkeypatch, isolated_memory):
    def forbidden(*args, **kwargs):
        pytest.fail("Opening or editing the notebook must not generate model output")
    monkeypatch.setattr(LLMService, "complete_chat", forbidden)
    team, user, conversation = _account(client)
    before = _list(client, conversation)
    assert before["automatic_memory"] is False
    assert before["markdown_enabled"] is True
    assert not isolated_memory.root.exists(), "Read-only GET must not create memory files"
    assert [doc["prompt_limit"] for doc in before["documents"]] == [6000, 6000, 3000]
    card = _create(client, conversation, title="  回复偏好  ")
    assert card["title"] == "回复偏好"
    assert card["scope_level"] == "space" and card["effective_status"] == "active"
    assert card["category"] == "preference" and card["source"] is None
    edited = client.patch(BASE + "/cards/" + card["memory_id"], json={
        "conversation_id": conversation["conversation_id"], "content": "先给结论，再解释", "status": "disabled",
    })
    assert edited.status_code == 200 and edited.json()["effective_status"] == "disabled"
    doc = next(doc for doc in before["documents"] if doc["kind"] == "long_term")
    saved = client.put(BASE + "/documents/long_term", json={
        "conversation_id": conversation["conversation_id"], "content": "- 我偏好中文", "expected_revision": doc["revision"],
    })
    assert saved.status_code == 200, saved.text
    assert isolated_memory.read_long_term(team, user, conversation["space_id"]) == "- 我偏好中文"
    assert saved.json()["revision"] != doc["revision"]
    removed = client.delete(BASE + "/cards/" + card["memory_id"], params={"conversation_id": conversation["conversation_id"]})
    assert removed.status_code == 204 and removed.content == b""
    assert _list(client, conversation)["items"] == []
    with SessionLocal() as db:
        assert db.get(MemoryCard, card["memory_id"]) is None
        assert db.query(MemoryCardEmbedding).filter_by(memory_id=card["memory_id"]).count() == 0


@pytest.mark.parametrize("field", ["title", "content", "category"])
def test_notebook_rejects_whitespace_only_card_values(client, field):
    _, _, conversation = _account(client)
    body = {"conversation_id": conversation["conversation_id"], "title": "标题", "content": "内容", field: "  \n "}
    assert client.post(BASE + "/cards", json=body).status_code == 422


def test_notebook_pagination_includes_global_and_only_current_space(client):
    team, user, conversation = _account(client)
    a = _create(client, conversation)
    b = _create(client, conversation, title="格式偏好")
    other_space = client.post("/api/v1/spaces", json={"name": "另一个项目"}).json()
    other = client.post("/api/v1/conversations", json={"space_id": other_space["space_id"]}).json()
    excluded = _create(client, other, title="另一个空间")
    with SessionLocal() as db:
        global_card = MemoryCard(
            memory_id=str(uuid4()), team_id=team, user_id=user, space_id=None, scope_level="global",
            title="全局偏好", content="中文优先", category="preference", status="active", confidence=1, weight=1,
        )
        db.add(global_card)
        db.commit()
        global_id = global_card.memory_id
    first = _list(client, conversation, limit=2)
    second = _list(client, conversation, offset=first["next_offset"], limit=2)
    assert first["has_more"] is True and second["has_more"] is False
    ids = {item["memory_id"] for item in first["items"] + second["items"]}
    assert ids == {a["memory_id"], b["memory_id"], global_id}
    assert excluded["memory_id"] not in ids
    assert client.patch(BASE + "/cards/" + global_id, json={
        "conversation_id": conversation["conversation_id"], "status": "disabled",
    }).status_code == 200
    assert client.patch(BASE + "/cards/" + excluded["memory_id"], json={
        "conversation_id": conversation["conversation_id"], "status": "disabled",
    }).status_code == 404


def test_same_team_other_user_cannot_read_or_change_cards_documents_or_conversation(client):
    team, user, conversation = _account(client)
    card = _create(client, conversation)
    register_workspace_user_in_team(client, team_id=team, prefix="notebook_other", display_name="Other")
    own = client.post("/api/v1/conversations", json={}).json()
    assert _list(client, own)["items"] == []
    assert client.get(BASE, params={"conversation_id": conversation["conversation_id"]}).status_code == 404
    assert client.patch(BASE + "/cards/" + card["memory_id"], json={
        "conversation_id": own["conversation_id"], "content": "越权修改",
    }).status_code == 404
    assert client.delete(BASE + "/cards/" + card["memory_id"], params={"conversation_id": own["conversation_id"]}).status_code == 404
    assert client.put(BASE + "/documents/self_model", json={
        "conversation_id": conversation["conversation_id"], "content": "越权修改", "expected_revision": "0" * 64,
    }).status_code == 404
    login_existing_user(client, user_id=user)
    assert _list(client, conversation)["items"][0]["content"] == "我喜欢简短回复"


def _message(team, user, conversation, *, request="个人来源", response="来源回复"):
    with SessionLocal() as db:
        item = ChatHistory(message_id=str(uuid4()), team_id=team, user_id=user,
            conversation_id=conversation["conversation_id"], space_id=conversation["space_id"],
            channel="companion", request_text=request, response_text=response,
            request_payload_json="{}", response_payload_json="{}")
        db.add(item)
        db.commit()
        return item.message_id


def test_source_preview_is_only_available_in_its_original_owned_conversation(client):
    team, user, conversation = _account(client)
    message_id = _message(team, user, conversation, request="真实偏好" * 100)
    card = _create(client, conversation, source_message_id=message_id)
    assert card["source"]["available"] is True
    assert len(card["source"]["request_preview"]) == 240
    another = client.post("/api/v1/conversations", json={}).json()
    listed = _list(client, another)["items"][0]
    assert listed["source"] == {"message_id": message_id, "available": False, "request_preview": "", "response_preview": ""}
    invalid = client.post(BASE + "/cards", json={
        "conversation_id": another["conversation_id"], "title": "标题", "content": "内容", "source_message_id": message_id,
    })
    assert invalid.status_code == 404


def test_corrupt_cross_user_source_reference_never_leaks_preview(client):
    team, user, conversation = _account(client)
    card = _create(client, conversation)
    _, other_user = register_workspace_user_in_team(client, team_id=team, prefix="source_other", display_name="Other")
    other_conversation = client.post("/api/v1/conversations", json={}).json()
    foreign_source = _message(team, other_user, other_conversation, request="PRIVATE-OTHER-REQUEST", response="PRIVATE-OTHER-RESPONSE")
    with SessionLocal() as db:
        db.get(MemoryCard, card["memory_id"]).source_message_id = foreign_source
        db.commit()
    login_existing_user(client, user_id=user)
    response = client.get(BASE, params={"conversation_id": conversation["conversation_id"]})
    assert response.status_code == 200
    assert "PRIVATE-OTHER" not in response.text
    assert response.json()["items"][0]["source"]["available"] is False


def test_effective_expiry_matches_sqlite_retrieval_and_disabled_cards(client):
    _, _, conversation = _account(client)
    expired = _create(client, conversation, title="已经到期")
    disabled = _create(client, conversation, title="已经暂停")
    with SessionLocal() as db:
        db.get(MemoryCard, expired["memory_id"]).expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        row = db.get(MemoryCard, disabled["memory_id"])
        row.status = "disabled"
        row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
    statuses = {row["memory_id"]: row["effective_status"] for row in _list(client, conversation)["items"]}
    assert statuses == {expired["memory_id"]: "expired", disabled["memory_id"]: "disabled"}


def test_document_compare_and_swap_preserves_recent_turns_and_rejects_stale_edit(client, isolated_memory):
    team, user, conversation = _account(client)
    space = conversation["space_id"]
    isolated_memory.write_recent_context(team, user, space, "旧背景\n\n<!-- BEGIN Recent Turns -->\n内部对话\n<!-- END Recent Turns -->")
    doc = next(doc for doc in _list(client, conversation)["documents"] if doc["kind"] == "recent_context")
    assert doc["content"] == "旧背景" and "内部对话" not in doc["content"]
    payload = {"conversation_id": conversation["conversation_id"], "content": "新背景", "expected_revision": doc["revision"]}
    saved = client.put(BASE + "/documents/recent_context", json=payload)
    assert saved.status_code == 200
    assert isolated_memory.read_recent_context(team, user, space, include_recent_turns=True) == (
        "新背景\n\n<!-- BEGIN Recent Turns -->\n内部对话\n<!-- END Recent Turns -->"
    )
    assert client.put(BASE + "/documents/recent_context", json=payload).status_code == 409
    assert isolated_memory.read_recent_context(team, user, space) == "新背景"
    forged = {**payload, "content": "<!-- BEGIN Recent Turns -->隐藏内容<!-- END Recent Turns -->", "expected_revision": saved.json()["revision"]}
    assert client.put(BASE + "/documents/recent_context", json=forged).status_code == 400
    clear = {**payload, "content": "", "expected_revision": saved.json()["revision"]}
    assert client.put(BASE + "/documents/recent_context", json=clear).status_code == 200
    assert "内部对话" in isolated_memory.read_recent_context(team, user, space, include_recent_turns=True)


def test_markdown_disabled_still_lists_cards_but_cannot_write_documents(client, monkeypatch, isolated_memory):
    _, _, conversation = _account(client)
    _create(client, conversation)
    monkeypatch.setenv("MEMORY_MARKDOWN_ENABLED", "false")
    reload_settings()
    listed = _list(client, conversation)
    assert len(listed["items"]) == 1 and listed["markdown_enabled"] is False
    assert all(doc["enabled"] is False for doc in listed["documents"])
    response = client.put(BASE + "/documents/long_term", json={
        "conversation_id": conversation["conversation_id"], "content": "禁止写入", "expected_revision": listed["documents"][1]["revision"],
    })
    assert response.status_code == 400 and not isolated_memory.root.exists()


def test_background_transform_merges_latest_editor_content_and_invalidates_old_revision(isolated_memory):
    snapshot = isolated_memory.snapshot_document("t", "u", "s", "long_term")
    saved = isolated_memory.update_document("t", "u", "s", "long_term", content="用户刚刚纠正的事实", expected_revision=snapshot["revision"])
    isolated_memory.transform_document("t", "u", "s", "long_term", lambda raw: raw + "\n其他渠道新事实")
    assert isolated_memory.read_long_term("t", "u", "s") == "用户刚刚纠正的事实\n\n其他渠道新事实"
    with pytest.raises(MemoryDocumentConflictError):
        isolated_memory.update_document("t", "u", "s", "long_term", content="过期编辑", expected_revision=saved["revision"])


def test_document_cas_serializes_independent_processes(tmp_path):
    root = tmp_path / "cross_process"
    store = MemoryMarkdownStore(str(root))
    revision = store.snapshot_document("t", "u", "s", "long_term")["revision"]
    gate = tmp_path / "start"
    script = """import sys,time
from pathlib import Path
from app.services.memory_markdown_store import MemoryMarkdownStore,MemoryDocumentConflictError
root,revision,gate,text=sys.argv[1:]
deadline=time.monotonic()+10
while not Path(gate).exists():
    if time.monotonic()>deadline: raise RuntimeError('gate timeout')
    time.sleep(.01)
try:
    MemoryMarkdownStore(root).update_document('t','u','s','long_term',content=text,expected_revision=revision)
    print('saved')
except MemoryDocumentConflictError:
    print('conflict')
"""
    children = [subprocess.Popen([sys.executable, "-c", script, str(root), revision, str(gate), text],
                cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for text in ("first editor", "second editor")]
    try:
        gate.write_text("go")
        outputs = [child.communicate(timeout=20) for child in children]
        assert all(child.returncode == 0 for child in children), outputs
        assert sorted(stdout.strip() for stdout, _ in outputs) == ["conflict", "saved"]
        assert store.read_long_term("t", "u", "s") in {"first editor", "second editor"}
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_embedding_failure_during_create_rolls_back_the_card_and_index(client, monkeypatch):
    team, user, conversation = _account(client)

    def offline(*args, **kwargs):
        raise httpx.ConnectError("The configured embedding provider is unavailable")

    monkeypatch.setattr(EmbeddingService, "embed_text", offline)
    with pytest.raises(httpx.ConnectError):
        client.post(BASE + "/cards", json={
            "conversation_id": conversation["conversation_id"],
            "title": "不能留下半张卡", "content": "索引和正文必须一起成功",
        })
    with SessionLocal() as db:
        assert db.query(MemoryCard).filter_by(team_id=team, user_id=user).count() == 0
        assert db.query(MemoryCardEmbedding).filter_by(team_id=team).count() == 0
    assert _list(client, conversation)["items"] == []


def test_embedding_failure_during_correction_preserves_original_fact_summary_and_index(client, monkeypatch):
    _, _, conversation = _account(client)
    card = _create(client, conversation)
    with SessionLocal() as db:
        original = db.get(MemoryCard, card["memory_id"])
        original.summary = "已有旧摘要，应随正文一起回滚"
        db.commit()
        embedding = db.query(MemoryCardEmbedding).filter_by(memory_id=card["memory_id"]).one()
        before_card = (original.title, original.content, original.summary, original.status, original.updated_at)
        before_index = (embedding.vector_json, embedding.embedding_model, embedding.vector_dim, embedding.updated_at)

    def offline(*args, **kwargs):
        raise httpx.ConnectError("The configured embedding provider is unavailable")

    monkeypatch.setattr(EmbeddingService, "embed_text", offline)
    with pytest.raises(httpx.ConnectError):
        client.patch(BASE + "/cards/" + card["memory_id"], json={
            "conversation_id": conversation["conversation_id"],
            "title": "新标题不能单独落盘", "content": "这条纠正尚未保存", "status": "disabled",
        })
    with SessionLocal() as db:
        unchanged = db.get(MemoryCard, card["memory_id"])
        index = db.query(MemoryCardEmbedding).filter_by(memory_id=card["memory_id"]).one()
        assert (unchanged.title, unchanged.content, unchanged.summary, unchanged.status, unchanged.updated_at) == before_card
        assert (index.vector_json, index.embedding_model, index.vector_dim, index.updated_at) == before_index


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("offset_hours", [8, -7, None], ids=["UTC+08", "UTC-07", "naive-UTC"])
@pytest.mark.parametrize("minutes", [-5, 5], ids=["expired-five-minutes", "expires-in-five-minutes"])
def test_expiry_offsets_match_notebook_keyword_and_semantic_retrieval(client, operation, offset_hours, minutes):
    team, user, conversation = _account(client)
    marker = "EXPIRY_" + uuid4().hex
    deadline_utc = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    supplied = (
        deadline_utc.replace(tzinfo=None) if offset_hours is None
        else deadline_utc.astimezone(timezone(timedelta(hours=offset_hours)))
    )
    body = {"space_id": conversation["space_id"], "title": marker, "content": marker, "category": "fact"}
    if operation == "create":
        response = client.post("/api/v1/memory/cards", json={**body, "expires_at": supplied.isoformat()})
        assert response.status_code == 201, response.text
        card = response.json()
    else:
        response = client.post("/api/v1/memory/cards", json=body)
        assert response.status_code == 201, response.text
        card = response.json()
        updated = client.patch("/api/v1/memory/cards/" + card["memory_id"], json={"expires_at": supplied.isoformat()})
        assert updated.status_code == 200, updated.text

    item = next(item for item in _list(client, conversation)["items"] if item["memory_id"] == card["memory_id"])
    assert item["effective_status"] == ("expired" if minutes < 0 else "active")
    response_deadline = datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00"))
    assert response_deadline.tzinfo is not None
    assert response_deadline == deadline_utc
    with SessionLocal() as db:
        stored = db.get(MemoryCard, card["memory_id"]).expires_at
        stored_utc = stored.replace(tzinfo=timezone.utc) if stored.tzinfo is None else stored.astimezone(timezone.utc)
        assert stored_utc == deadline_utc
        graph = deps.create_background_agent_service(db)
        keyword = graph.tool_service.execute(team_id=team, user_id=user, action="recall_memory", arguments={
            "space_id": conversation["space_id"], "query": marker,
        })["memories"]
        semantic = graph.rag_chat_service.memory_service.search_cards_for_chat(
            team_id=team, user_id=user, space_id=conversation["space_id"], query=marker,
        )
        expected_ids = {card["memory_id"]} if minutes > 0 else set()
        assert {item["memory_id"] for item in keyword} == expected_ids
        assert {item["memory_id"] for item in semantic} == expected_ids
