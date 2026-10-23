"""Tool retrieval must respect the same private ownership as the web APIs."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.exceptions import EntityNotFoundError
from app.db.session import SessionLocal
from app.models.chat_history import ChatHistory
from app.models.document import Document
from app.models.memory_card import MemoryCard
from app.models.project_space import ProjectSpace
from app.services.tool_catalog_service import ToolCatalogService
from app.services.tool_safety import ToolSafetyService
from app.services.tool_service import ToolService
from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


@pytest.fixture
def private_workspace_data(client):
    team_id, first_user = register_workspace_user(
        client, prefix=f"privacy_{uuid4().hex[:6]}", display_name="First friend",
    )
    first = client.post("/api/v1/conversations", json={"title": "First private chat"}).json()
    _, second_user = register_workspace_user_in_team(
        client, team_id=team_id, prefix=f"privacy_other_{uuid4().hex[:6]}", display_name="Second friend",
    )
    second = client.post("/api/v1/conversations", json={"title": "Second private chat"}).json()

    with SessionLocal() as db:
        deleted_space = ProjectSpace(
            space_id=str(uuid4()), team_id=team_id, owner_user_id=first_user,
            name="Deleted private space", status="deleted",
        )
        db.add(deleted_space)
        db.flush()
        for label, user_id, conversation in (("mine", first_user, first), ("theirs", second_user, second)):
            for visibility, conversation_id in (("conversation", conversation["conversation_id"]), ("library", None)):
                db.add(Document(
                    document_id=str(uuid4()), team_id=team_id, space_id=conversation["space_id"],
                    conversation_id=conversation_id, visibility=visibility,
                    source_name=f"privacy-{label}-{visibility}", content="privacy private document",
                    content_type="txt", status="ready",
                ))
            for scope, space_id in (("space", conversation["space_id"]), ("global", None)):
                db.add(MemoryCard(
                    memory_id=str(uuid4()), team_id=team_id, user_id=user_id, space_id=space_id,
                    scope_level=scope, title=f"privacy-{label}-{scope}", content="privacy private memory",
                ))
            db.add(ChatHistory(
                message_id=str(uuid4()), team_id=team_id, user_id=user_id,
                space_id=conversation["space_id"], conversation_id=conversation["conversation_id"],
                channel="companion", request_text=f"privacy-{label}", response_text=f"reply-{label}",
                request_payload_json="{}", response_payload_json="{}",
            ))
        # Unscoped documents deliberately preserve the existing shared team library.
        db.add(Document(
            document_id=str(uuid4()), team_id=team_id, space_id=None, conversation_id=None,
            source_name="privacy-shared-library", content="privacy shared reference", content_type="txt", status="ready",
        ))
        db.add(Document(
            document_id=str(uuid4()), team_id=team_id, space_id=deleted_space.space_id,
            source_name="privacy-deleted-space", content="privacy deleted reference", content_type="txt", status="ready",
        ))
        for title, space_id in (("privacy-deleted", deleted_space.space_id), ("privacy-inaccessible", second["space_id"])):
            db.add(MemoryCard(
                memory_id=str(uuid4()), team_id=team_id, user_id=first_user, space_id=space_id,
                scope_level="space", title=title, content="privacy inaccessible memory",
            ))
        db.commit()
    return {
        "team_id": team_id, "user_id": first_user,
        "own_space": first["space_id"], "foreign_space": second["space_id"],
        "own_conversation": first["conversation_id"], "foreign_conversation": second["conversation_id"],
    }


def _service(db):
    return ToolService(db=db, catalog=ToolCatalogService(), safety=ToolSafetyService())


@pytest.mark.parametrize("tool,result_key", [("search_knowledge", "matches"), ("list_recent_documents", "documents")])
def test_document_tools_without_space_never_return_teammate_documents(private_workspace_data, tool, result_key):
    data = private_workspace_data
    with SessionLocal() as db:
        result = _service(db).execute(
            team_id=data["team_id"], user_id=data["user_id"], action=tool,
            arguments={"query": "privacy", "limit": 20} if tool == "search_knowledge" else {"limit": 20},
        )
    assert {item["source_name"] for item in result[result_key]} == {
        "privacy-mine-conversation", "privacy-mine-library", "privacy-shared-library",
    }


def test_memory_recall_without_space_is_private_and_skips_inaccessible_spaces(private_workspace_data):
    data = private_workspace_data
    with SessionLocal() as db:
        result = _service(db).execute(
            team_id=data["team_id"], user_id=data["user_id"], action="recall_memory",
            arguments={"query": "privacy", "limit": 20},
        )
    assert {item["title"] for item in result["memories"]} == {"privacy-mine-space", "privacy-mine-global"}


def test_forget_memory_cannot_delete_another_teammates_card(private_workspace_data):
    data = private_workspace_data
    with SessionLocal() as db:
        foreign = db.scalar(select(MemoryCard).where(
            MemoryCard.team_id == data["team_id"], MemoryCard.user_id != data["user_id"],
        ))
        foreign_id = foreign.memory_id
        with pytest.raises(EntityNotFoundError):
            _service(db).execute(
                team_id=data["team_id"], user_id=data["user_id"], action="forget_memory",
                arguments={"memory_id": foreign_id}, confirmed=True,
            )
        assert db.get(MemoryCard, foreign_id) is not None


@pytest.mark.parametrize("tool", ["recall_memory", "search_knowledge", "list_recent_documents"])
def test_explicit_foreign_space_is_hidden_from_retrieval_tools(private_workspace_data, tool):
    data = private_workspace_data
    with SessionLocal() as db:
        with pytest.raises(EntityNotFoundError):
            _service(db).execute(
                team_id=data["team_id"], user_id=data["user_id"], action=tool,
                arguments={"space_id": data["foreign_space"], **({"query": "privacy"} if tool != "list_recent_documents" else {})},
            )


def test_message_lookup_is_private_and_refuses_foreign_conversations(private_workspace_data):
    data = private_workspace_data
    with SessionLocal() as db:
        service = _service(db)
        result = service.execute(
            team_id=data["team_id"], user_id=data["user_id"], action="message_lookup", arguments={"limit": 20},
        )
        assert [item["request"] for item in result["messages"]] == ["privacy-mine"]
        with pytest.raises(EntityNotFoundError):
            service.execute(
                team_id=data["team_id"], user_id=data["user_id"], action="message_lookup",
                arguments={"conversation_id": data["foreign_conversation"]},
            )
