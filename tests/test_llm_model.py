from uuid import uuid4

from app.core.config import get_settings
from tests.auth_helpers import register_workspace_user


def test_llm_model_routes_require_authenticated_user(client) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/llm/models")
    assert response.status_code == 401


def test_llm_model_config_is_isolated_by_account(client) -> None:
    suffix = uuid4().hex[:8]
    register_workspace_user(
        client,
        prefix=f"llm_a_{suffix}",
        display_name="LLM User A",
    )

    upsert = client.post(
        "/api/v1/llm/models",
        json={
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test-account-a",
        },
    )
    assert upsert.status_code == 200
    body = upsert.json()
    assert body["model_name"] == "gpt-4.1-mini"
    assert body["base_url"] == "https://api.openai.com/v1"
    assert body["has_api_key"] is True
    assert body["masked_api_key"] != "sk-test-account-a"

    list_a = client.get("/api/v1/llm/models")
    assert list_a.status_code == 200
    list_a_body = list_a.json()
    settings = get_settings()
    assert list_a_body["default_model"]["model_name"] == settings.llm_model
    assert list_a_body["default_model"]["provider"] == settings.llm_provider
    assert len(list_a_body["items"]) == 1
    assert list_a_body["items"][0]["model_name"] == "gpt-4.1-mini"

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 204

    register_workspace_user(
        client,
        prefix=f"llm_b_{suffix}",
        display_name="LLM User B",
    )

    list_b = client.get("/api/v1/llm/models")
    assert list_b.status_code == 200
    assert list_b.json()["items"] == []


def test_llm_model_config_can_be_deleted(client) -> None:
    suffix = uuid4().hex[:8]
    register_workspace_user(
        client,
        prefix=f"llm_del_{suffix}",
        display_name="LLM Delete User",
    )

    upsert = client.post(
        "/api/v1/llm/models",
        json={
            "model_name": "gpt-5-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test-delete",
        },
    )
    assert upsert.status_code == 200

    delete = client.delete("/api/v1/llm/models/gpt-5-mini")
    assert delete.status_code == 204

    list_after = client.get("/api/v1/llm/models")
    assert list_after.status_code == 200
    assert list_after.json()["items"] == []


def test_llm_model_base_url_strips_endpoint_suffixes(client) -> None:
    suffix = uuid4().hex[:8]
    register_workspace_user(
        client,
        prefix=f"llm_suffix_{suffix}",
        display_name="LLM Suffix User",
    )

    response = client.post(
        "/api/v1/llm/models",
        json={
            "model_name": "doubao-1",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3/responses/chat/completions",
            "api_key": "sk-test-1234",
        },
    )

    assert response.status_code == 200
    assert response.json()["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
