from tests.auth_helpers import register_workspace_user


def _admin_headers() -> dict[str, str]:
    return {"X-Dev-Admin-Token": "test-admin-token"}


def test_mcp_reload_requires_dev_admin_token(client) -> None:
    register_workspace_user(
        client,
        prefix="mcp_reload",
        display_name="MCP Reload User",
    )

    without_admin = client.post("/api/v1/mcp/servers/reload")
    assert without_admin.status_code == 403

    with_admin = client.post("/api/v1/mcp/servers/reload", headers=_admin_headers())
    assert with_admin.status_code == 200
    assert with_admin.json()["status"] in {"no_config", "ok"}
