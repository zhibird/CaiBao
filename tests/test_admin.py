from uuid import uuid4

from tests.auth_helpers import register_workspace_user


def _admin_headers() -> dict[str, str]:
    return {"X-Dev-Admin-Token": "test-admin-token"}


def _create_team_user_conversation_with_doc(client, suffix: str) -> tuple[str, str, str, str]:
    team_id, user_id = register_workspace_user(
        client,
        prefix=f"admin_{suffix}",
        display_name="Admin Test User",
    )

    conversation_response = client.post(
        "/api/v1/conversations",
        json={
            "title": "Admin Conversation",
        },
    )
    assert conversation_response.status_code == 201
    conversation_id = conversation_response.json()["conversation_id"]

    document_response = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "source_name": "admin-note.md",
            "content_type": "md",
            "content": "# note\n\nadmin test content",
        },
    )
    assert document_response.status_code == 201
    document_id = document_response.json()["document_id"]

    echo_response = client.post(
        "/api/v1/chat/echo",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "message": "hello admin",
        },
    )
    assert echo_response.status_code == 200

    return team_id, user_id, conversation_id, document_id


def test_admin_session_requires_token(client) -> None:
    without_token = client.get("/api/v1/admin/session")
    assert without_token.status_code == 403

    with_token = client.get("/api/v1/admin/session", headers=_admin_headers())
    assert with_token.status_code == 200
    assert with_token.json()["account_id"] == "dev_admin_test"
    assert with_token.json()["role"] == "admin"


def test_admin_lists_and_deletes_conversation_cascade(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id, document_id = _create_team_user_conversation_with_doc(client, suffix)

    list_conversations = client.get(
        "/api/v1/admin/conversations",
        params={"team_id": team_id, "user_id": user_id},
        headers=_admin_headers(),
    )
    assert list_conversations.status_code == 200
    items = list_conversations.json()
    target = next((item for item in items if item["conversation_id"] == conversation_id), None)
    assert target is not None
    assert target["message_count"] >= 1
    assert target["document_count"] >= 1

    delete_response = client.delete(
        f"/api/v1/admin/conversations/{conversation_id}",
        headers=_admin_headers(),
    )
    assert delete_response.status_code == 204

    list_docs_after = client.get(
        "/api/v1/admin/documents",
        params={"team_id": team_id},
        headers=_admin_headers(),
    )
    assert list_docs_after.status_code == 200
    listed_doc_ids = [item["document_id"] for item in list_docs_after.json()]
    assert document_id not in listed_doc_ids


def test_admin_update_role_and_delete_user(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, _, _ = _create_team_user_conversation_with_doc(client, suffix)

    update_role = client.patch(
        f"/api/v1/admin/users/{user_id}/role",
        json={"role": "team_admin"},
        headers=_admin_headers(),
    )
    assert update_role.status_code == 200
    assert update_role.json()["role"] == "team_admin"

    delete_user = client.delete(
        f"/api/v1/admin/users/{user_id}",
        headers=_admin_headers(),
    )
    assert delete_user.status_code == 204

    get_user = client.get(f"/api/v1/users/{user_id}", headers=_admin_headers())
    assert get_user.status_code == 404

    users_in_team = client.get(
        "/api/v1/admin/users",
        params={"team_id": team_id},
        headers=_admin_headers(),
    )
    assert users_in_team.status_code == 200
    assert all(item["user_id"] != user_id for item in users_in_team.json())


def test_admin_cannot_delete_configured_admin_account(client) -> None:
    delete_admin_user = client.delete(
        "/api/v1/admin/users/dev_admin_test",
        headers=_admin_headers(),
    )
    assert delete_admin_user.status_code == 400

    delete_admin_team = client.delete(
        "/api/v1/admin/teams/dev_admin_test",
        headers=_admin_headers(),
    )
    assert delete_admin_team.status_code == 400
