from uuid import uuid4

from tests.auth_helpers import login_existing_user


def _admin_headers() -> dict[str, str]:
    return {"X-Dev-Admin-Token": "test-admin-token"}


def test_bootstrap_routes_require_admin_token(client) -> None:
    assert client.get("/api/v1/teams").status_code == 403
    assert client.get("/api/v1/users").status_code == 403


def test_create_team_and_user(client) -> None:
    suffix = uuid4().hex[:8]
    team_id = f"team_{suffix}"
    user_id = f"u_{suffix}"

    team_response = client.post(
        "/api/v1/teams",
        headers=_admin_headers(),
        json={
            "team_id": team_id,
            "name": "Operations",
            "description": "MVP team",
        },
    )
    assert team_response.status_code == 201
    assert team_response.json()["team_id"] == team_id

    user_response = client.post(
        "/api/v1/users",
        headers=_admin_headers(),
        json={
            "user_id": user_id,
            "team_id": team_id,
            "display_name": "Bob",
            "role": "member",
        },
    )
    assert user_response.status_code == 201
    assert user_response.json()["team_id"] == team_id

    get_user_response = client.get(f"/api/v1/users/{user_id}", headers=_admin_headers())
    assert get_user_response.status_code == 200
    assert get_user_response.json()["display_name"] == "Bob"


def test_ensure_team_and_user_are_idempotent(client) -> None:
    suffix = uuid4().hex[:8]
    team_id = f"workspace_{suffix}"
    user_id = f"workspace_user_{suffix}"

    create_team_response = client.put(
        f"/api/v1/teams/{team_id}",
        headers=_admin_headers(),
        json={
            "name": "Workspace Alpha",
            "description": None,
        },
    )
    assert create_team_response.status_code == 201
    assert create_team_response.json()["team_id"] == team_id
    assert create_team_response.json()["name"] == "Workspace Alpha"

    reuse_team_response = client.put(
        f"/api/v1/teams/{team_id}",
        headers=_admin_headers(),
        json={
            "name": "Workspace Beta",
            "description": None,
        },
    )
    assert reuse_team_response.status_code == 200
    assert reuse_team_response.json()["team_id"] == team_id
    assert reuse_team_response.json()["name"] == "Workspace Beta"

    create_user_response = client.put(
        f"/api/v1/users/{user_id}",
        headers=_admin_headers(),
        json={
            "team_id": team_id,
            "display_name": "Workspace Beta",
        },
    )
    assert create_user_response.status_code == 201
    assert create_user_response.json()["user_id"] == user_id
    assert create_user_response.json()["team_id"] == team_id
    assert create_user_response.json()["display_name"] == "Workspace Beta"

    reuse_user_response = client.put(
        f"/api/v1/users/{user_id}",
        headers=_admin_headers(),
        json={
            "team_id": team_id,
            "display_name": "Workspace Gamma",
        },
    )
    assert reuse_user_response.status_code == 200
    assert reuse_user_response.json()["user_id"] == user_id
    assert reuse_user_response.json()["team_id"] == team_id
    assert reuse_user_response.json()["display_name"] == "Workspace Gamma"


def test_ensure_user_rejects_cross_team_reuse(client) -> None:
    suffix = uuid4().hex[:8]
    first_team_id = f"team_a_{suffix}"
    second_team_id = f"team_b_{suffix}"
    user_id = f"user_{suffix}"

    first_team_response = client.put(
        f"/api/v1/teams/{first_team_id}",
        headers=_admin_headers(),
        json={
            "name": "Team A",
            "description": None,
        },
    )
    assert first_team_response.status_code == 201

    second_team_response = client.put(
        f"/api/v1/teams/{second_team_id}",
        headers=_admin_headers(),
        json={
            "name": "Team B",
            "description": None,
        },
    )
    assert second_team_response.status_code == 201

    create_user_response = client.put(
        f"/api/v1/users/{user_id}",
        headers=_admin_headers(),
        json={
            "team_id": first_team_id,
            "display_name": "Owner",
        },
    )
    assert create_user_response.status_code == 201

    conflict_response = client.put(
        f"/api/v1/users/{user_id}",
        headers=_admin_headers(),
        json={
            "team_id": second_team_id,
            "display_name": "Owner",
        },
    )
    assert conflict_response.status_code == 409
    assert conflict_response.json()["detail"] == (
        f"User '{user_id}' already exists in team '{first_team_id}'."
    )


def test_ensure_routes_preserve_id_length_validation(client) -> None:
    too_long_team_id = "t" * 65
    too_long_user_id = "u" * 65

    team_response = client.put(
        f"/api/v1/teams/{too_long_team_id}",
        headers=_admin_headers(),
        json={
            "name": "Too Long",
            "description": None,
        },
    )
    assert team_response.status_code == 422

    user_response = client.put(
        f"/api/v1/users/{too_long_user_id}",
        headers=_admin_headers(),
        json={
            "team_id": "team_short",
            "display_name": "Too Long",
        },
    )
    assert user_response.status_code == 422


def test_workspace_bootstrap_sequence_is_repeatable_without_conflicts(client) -> None:
    suffix = uuid4().hex[:8]
    workspace_id = f"workspace_{suffix}"
    workspace_name = "Repeatable Workspace"

    first_team_response = client.put(
        f"/api/v1/teams/{workspace_id}",
        headers=_admin_headers(),
        json={
            "name": workspace_name,
            "description": None,
        },
    )
    assert first_team_response.status_code == 201

    first_user_response = client.put(
        f"/api/v1/users/{workspace_id}",
        headers=_admin_headers(),
        json={
            "team_id": workspace_id,
            "display_name": workspace_name,
        },
    )
    assert first_user_response.status_code == 201

    second_team_response = client.put(
        f"/api/v1/teams/{workspace_id}",
        headers=_admin_headers(),
        json={
            "name": workspace_name,
            "description": None,
        },
    )
    assert second_team_response.status_code == 200

    second_user_response = client.put(
        f"/api/v1/users/{workspace_id}",
        headers=_admin_headers(),
        json={
            "team_id": workspace_id,
            "display_name": workspace_name,
        },
    )
    assert second_user_response.status_code == 200

    logged_in_team_id, logged_in_user_id = login_existing_user(client, user_id=workspace_id)
    assert logged_in_team_id == workspace_id
    assert logged_in_user_id == workspace_id

    llm_response = client.get(
        "/api/v1/llm/models",
    )
    assert llm_response.status_code == 200

    embedding_response = client.get(
        "/api/v1/embedding/models",
    )
    assert embedding_response.status_code == 200

    conversations_response = client.get(
        "/api/v1/conversations",
        params={"limit": 100},
    )
    assert conversations_response.status_code == 200
