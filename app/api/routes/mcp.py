from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.deps import (
    get_admin_service,
    get_mcp_manager,
    get_tool_catalog_service,
    get_tool_service,
    require_current_active_user,
    require_dev_admin,
)
from app.api.deps import (
    _finalize_mcp_reload,
    _reset_mcp_connection as _reset_mcp,
)
from app.core.exceptions import DomainValidationError
from app.models.user import User
from app.services.admin_service import AdminService
from app.services.mcp_manager import MCPManager
from app.services.tool_catalog_service import ToolCatalogService
from app.services.tool_service import ToolService

router = APIRouter(prefix="/mcp")


@router.get("/servers")
def list_mcp_servers(
    mcp_manager: MCPManager = Depends(get_mcp_manager),
    current_user: User = Depends(require_current_active_user),
):
    try:
        return mcp_manager.status()
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/servers/reload")
def reload_mcp_servers(
    mcp_manager: MCPManager = Depends(get_mcp_manager),
    catalog: ToolCatalogService = Depends(get_tool_catalog_service),
    current_user: User = Depends(require_current_active_user),
    admin_user: User = Depends(require_dev_admin),
):
    try:
        _reset_mcp()
        mcp_manager.shutdown_all()
        configs = mcp_manager.load_config()
        if not configs:
            # No config → clear all MCP tools and cache
            _finalize_mcp_reload(mcp_manager, catalog)
            return {"servers_loaded": 0, "tools_discovered": 0, "status": "no_config"}

        mcp_manager.connect_all(configs)
        _finalize_mcp_reload(mcp_manager, catalog)

        mcp_tool_count = sum(
            1 for d in catalog.list_definitions() if d.source == "mcp"
        )
        return {
            "servers_loaded": len(configs),
            "tools_discovered": mcp_tool_count,
            "status": "ok",
        }
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/tools")
def list_mcp_tools(
    catalog: ToolCatalogService = Depends(get_tool_catalog_service),
    current_user: User = Depends(require_current_active_user),
):
    try:
        all_defs = catalog.list_definitions()
        mcp_tools = [d for d in all_defs if d.source == "mcp"]
        return [
            {
                "name": d.name,
                "display_name": d.display_name,
                "description": d.description,
                "dangerous": d.dangerous,
                "provider": d.provider,
                "input_schema": d.input_schema,
            }
            for d in mcp_tools
        ]
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/tools/{tool_name}/test")
def test_mcp_tool(
    tool_name: str,
    body: dict,
    tool_service: ToolService = Depends(get_tool_service),
    catalog: ToolCatalogService = Depends(get_tool_catalog_service),
    admin_service: AdminService = Depends(get_admin_service),
    current_user: User = Depends(require_current_active_user),
    x_dev_admin_token: str | None = Header(default=None, alias="X-Dev-Admin-Token"),
):
    try:
        arguments = body.get("arguments", {}) if isinstance(body, dict) else {}
        dry_run = bool(body.get("dry_run", False)) if isinstance(body, dict) else False

        definition = catalog.get_definition(tool_name)
        if definition is None:
            raise DomainValidationError(f"Unknown tool: {tool_name}")

        is_admin = False
        if x_dev_admin_token:
            try:
                admin_service.authenticate(x_dev_admin_token)
                is_admin = True
            except DomainValidationError:
                pass

        # Dangerous tools require dry_run=True unless caller is a dev admin
        if definition.dangerous and not dry_run and not is_admin:
            raise DomainValidationError(
                f"Tool '{tool_name}' is dangerous. Set dry_run=true to test it, "
                "or use a valid X-Dev-Admin-Token."
            )

        result = tool_service.execute(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            action=tool_name,
            arguments=arguments,
            confirmed=True,
            dry_run=dry_run,
        )
        return result
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
