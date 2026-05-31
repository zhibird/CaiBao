from uuid import uuid4

from tests.auth_helpers import register_workspace_user


def test_embedding_model_routes_require_authenticated_user(client) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/embedding/models")
    assert response.status_code == 401


def test_embedding_model_config_is_isolated_by_account(client) -> None:
    suffix = uuid4().hex[:8]
    register_workspace_user(
        client,
        prefix=f"embedding_a_{suffix}",
        display_name="Embedding User A",
    )

    upsert = client.post(
        "/api/v1/embedding/models",
        json={
            "model_name": "text-embedding-3-small",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-embedding-account-a",
        },
    )
    assert upsert.status_code == 200
    body = upsert.json()
    assert body["model_name"] == "text-embedding-3-small"
    assert body["provider"] == "openai"
    assert body["base_url"] == "https://api.openai.com/v1"
    assert body["has_api_key"] is True
    assert body["masked_api_key"] != "sk-embedding-account-a"

    list_a = client.get("/api/v1/embedding/models")
    assert list_a.status_code == 200
    list_a_body = list_a.json()
    assert list_a_body["default_model"]["model_name"] == "hashing_v1"
    assert list_a_body["default_model"]["provider"] == "mock"
    assert len(list_a_body["items"]) == 1
    assert list_a_body["items"][0]["model_name"] == "text-embedding-3-small"

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 204

    register_workspace_user(
        client,
        prefix=f"embedding_b_{suffix}",
        display_name="Embedding User B",
    )

    list_b = client.get("/api/v1/embedding/models")
    assert list_b.status_code == 200
    assert list_b.json()["items"] == []


def test_embedding_model_config_can_be_deleted(client) -> None:
    suffix = uuid4().hex[:8]
    register_workspace_user(
        client,
        prefix=f"embedding_del_{suffix}",
        display_name="Embedding Delete User",
    )

    upsert = client.post(
        "/api/v1/embedding/models",
        json={
            "model_name": "emb-2026-02",
            "provider": "volcengine",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": "embedding-key-delete",
        },
    )
    assert upsert.status_code == 200

    delete = client.delete("/api/v1/embedding/models/emb-2026-02")
    assert delete.status_code == 204

    list_after = client.get("/api/v1/embedding/models")
    assert list_after.status_code == 200
    assert list_after.json()["items"] == []


def test_embedding_model_rejects_reserved_mock_name(client) -> None:
    suffix = uuid4().hex[:8]
    register_workspace_user(
        client,
        prefix=f"embedding_reserved_{suffix}",
        display_name="Embedding Reserved User",
    )

    upsert = client.post(
        "/api/v1/embedding/models",
        json={
            "model_name": "mock",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-embedding-account-a",
        },
    )
    assert upsert.status_code == 400
