from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.agent_app import AgentApp
from app.models.agent_app_version import AgentAppVersion
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.schemas.agent_app import AgentAppCreate, AgentAppInvokeRequest, AgentAppUpdate
from app.schemas.agent_app import _sanitize_llm_routing as _sanitize_llm_routing_output
from app.services.agent_service import AgentService
from app.services.document_service import DocumentService

from app.services.user_service import UserService


class AgentAppService:
    _ALLOWED_MODES = {"agent_auto", "workflow", "claude_code", "codex", "pi"}
    _ALLOWED_STATUSES = {"draft", "published", "archived"}

    def __init__(
        self,
        *,
        db: Session,
        user_service: UserService,
        document_service: DocumentService,
        agent_service: AgentService,
    ) -> None:
        self.db = db
        self.user_service = user_service
        self.document_service = document_service
        self.agent_service = agent_service

    def create(self, payload: AgentAppCreate) -> AgentApp:
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        space_id = self._resolve_space_id(team_id=team_id, user_id=user_id, space_id=payload.space_id)
        now = datetime.now(timezone.utc)

        app = AgentApp(
            app_id=str(uuid4()),
            team_id=team_id,
            created_by_user_id=user_id,
            name=payload.name.strip(),
            description=payload.description.strip(),
            mode=self._normalize_mode(payload.mode),
            system_prompt=payload.system_prompt.strip(),
            model=self._clean_optional(payload.model),
            embedding_model=self._clean_optional(payload.embedding_model),
            space_id=space_id,
            retrieval_config_json=self._to_json(self._normalize_retrieval_config(payload.retrieval_config)),
            tool_config_json=self._to_json(self._normalize_tool_config(payload.tool_config)),
            workflow_config_json=self._to_json(self._normalize_workflow_config(payload.workflow_config)),
            llm_routing_json=self._to_json(self._normalize_llm_routing(payload.llm_routing)),
            status=self._normalize_status(payload.status),
            app_version=0,
            created_at=now,
            updated_at=now,
        )
        self.db.add(app)
        self.db.commit()
        self.db.refresh(app)
        return app

    def list(self, *, team_id: str, user_id: str, include_archived: bool = False) -> list[AgentApp]:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        stmt = select(AgentApp).where(AgentApp.team_id == team_id, AgentApp.created_by_user_id == user_id)
        if not include_archived:
            stmt = stmt.where(AgentApp.status != "archived")
        stmt = stmt.order_by(AgentApp.updated_at.desc(), AgentApp.created_at.desc())
        return list(self.db.scalars(stmt).all())

    def get(self, *, app_id: str, team_id: str, user_id: str) -> AgentApp:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        app = self.db.get(AgentApp, app_id)
        if app is None or app.team_id != team_id or app.created_by_user_id != user_id:
            raise EntityNotFoundError(f"Agent app '{app_id}' not found.")
        return app

    def update(self, *, app_id: str, payload: AgentAppUpdate) -> AgentApp:
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        app = self.get(app_id=app_id, team_id=team_id, user_id=user_id)

        if payload.name is not None:
            app.name = payload.name.strip()
        if payload.description is not None:
            app.description = payload.description.strip()
        if payload.mode is not None:
            app.mode = self._normalize_mode(payload.mode)
        if payload.system_prompt is not None:
            app.system_prompt = payload.system_prompt.strip()
        if payload.model is not None:
            app.model = self._clean_optional(payload.model)
        if payload.embedding_model is not None:
            app.embedding_model = self._clean_optional(payload.embedding_model)
        if payload.space_id is not None:
            app.space_id = self._resolve_space_id(team_id=team_id, user_id=user_id, space_id=payload.space_id)
        if payload.retrieval_config is not None:
            app.retrieval_config_json = self._to_json(self._normalize_retrieval_config(payload.retrieval_config))
        if payload.tool_config is not None:
            app.tool_config_json = self._to_json(self._normalize_tool_config(payload.tool_config))
        if payload.workflow_config is not None:
            app.workflow_config_json = self._to_json(self._normalize_workflow_config(payload.workflow_config))
        if payload.llm_routing is not None:
            app.llm_routing_json = self._to_json(self._normalize_llm_routing(payload.llm_routing))
        if payload.status is not None:
            app.status = self._normalize_status(payload.status)

        app.updated_at = datetime.now(timezone.utc)
        self.db.add(app)
        self.db.commit()
        self.db.refresh(app)
        return app

    def publish(self, *, app_id: str, team_id: str, user_id: str, notes: str = "") -> AgentAppVersion:
        app = self.get(app_id=app_id, team_id=team_id, user_id=user_id)
        latest = self.db.scalar(
            select(func.max(AgentAppVersion.version_number)).where(AgentAppVersion.app_id == app.app_id)
        )
        version_number = int(latest or 0) + 1
        snapshot = self._app_snapshot(app, version_number=version_number)
        version = AgentAppVersion(
            version_id=str(uuid4()),
            app_id=app.app_id,
            team_id=team_id,
            version_number=version_number,
            snapshot_json=self._to_json(snapshot),
            notes=notes.strip(),
            created_by_user_id=user_id,
            created_at=datetime.now(timezone.utc),
        )
        app.status = "published"
        app.app_version = version_number
        app.updated_at = datetime.now(timezone.utc)
        self.db.add(version)
        self.db.add(app)
        self.db.commit()
        self.db.refresh(version)
        return version

    def invoke(self, *, app_id: str, payload: AgentAppInvokeRequest) -> AgentRunResponse:
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        app = self.get(app_id=app_id, team_id=team_id, user_id=user_id)
        if app.status == "archived":
            raise DomainValidationError("Archived agent apps cannot be invoked.")

        retrieval_config = self._safe_json_dict(app.retrieval_config_json)
        tool_config = self._safe_json_dict(app.tool_config_json)
        workflow_config = self._safe_json_dict(app.workflow_config_json)
        space_id = payload.space_id or app.space_id
        if space_id is not None:
            space_id = self._resolve_space_id(team_id=team_id, user_id=user_id, space_id=space_id)

        danger_requires_confirmation = bool(tool_config.get("dangerous_requires_confirmation", True))
        effective_confirm = payload.confirm_dangerous_actions or not danger_requires_confirmation

        agent_payload = AgentRunRequest(
            team_id=team_id,
            user_id=user_id,
            conversation_id=payload.conversation_id,
            space_id=space_id,
            app_id=app.app_id,
            app_version=app.app_version,
            trigger_channel="app",
            run_mode=app.mode,
            system_prompt=app.system_prompt,
            task=payload.task,
            top_k=int(payload.top_k or retrieval_config.get("top_k") or 5),
            selected_document_ids=payload.selected_document_ids,
            include_memory=self._coalesce_bool(payload.include_memory, retrieval_config.get("include_memory"), True),
            include_library=self._coalesce_bool(payload.include_library, retrieval_config.get("include_library"), True),
            include_conclusions=self._coalesce_bool(
                payload.include_conclusions,
                retrieval_config.get("include_conclusions"),
                False,
            ),
            dry_run=payload.dry_run,
            confirm_dangerous_actions=effective_confirm,
            max_steps=int(payload.max_steps or workflow_config.get("max_steps") or 5),
            model=payload.model or app.model,
            embedding_model=payload.embedding_model or app.embedding_model,
            enabled_tools=self._enabled_tools(tool_config),
            workflow_config=workflow_config,
            model_routes=self._safe_json_dict(app.llm_routing_json) or None,
        )
        return self.agent_service.run(agent_payload)

    def invoke_queued(self, *, app_id: str, payload: AgentAppInvokeRequest) -> AgentRunStartResponse:
        """Create a queued run for this app (for streaming consumption)."""
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        app = self.get(app_id=app_id, team_id=team_id, user_id=user_id)
        if app.status == "archived":
            raise DomainValidationError("Archived agent apps cannot be invoked.")

        retrieval_config = self._safe_json_dict(app.retrieval_config_json)
        tool_config = self._safe_json_dict(app.tool_config_json)
        workflow_config = self._safe_json_dict(app.workflow_config_json)
        space_id = payload.space_id or app.space_id
        if space_id is not None:
            space_id = self._resolve_space_id(team_id=team_id, user_id=user_id, space_id=space_id)

        danger_requires_confirmation = bool(tool_config.get("dangerous_requires_confirmation", True))
        effective_confirm = payload.confirm_dangerous_actions or not danger_requires_confirmation

        agent_payload = AgentRunRequest(
            team_id=team_id,
            user_id=user_id,
            conversation_id=payload.conversation_id,
            space_id=space_id,
            app_id=app.app_id,
            app_version=app.app_version,
            trigger_channel="app",
            run_mode=app.mode,
            system_prompt=app.system_prompt,
            task=payload.task,
            top_k=int(payload.top_k or retrieval_config.get("top_k") or 5),
            selected_document_ids=payload.selected_document_ids,
            include_memory=self._coalesce_bool(payload.include_memory, retrieval_config.get("include_memory"), True),
            include_library=self._coalesce_bool(payload.include_library, retrieval_config.get("include_library"), True),
            include_conclusions=self._coalesce_bool(
                payload.include_conclusions,
                retrieval_config.get("include_conclusions"),
                False,
            ),
            dry_run=payload.dry_run,
            confirm_dangerous_actions=effective_confirm,
            max_steps=int(payload.max_steps or workflow_config.get("max_steps") or 5),
            model=payload.model or app.model,
            embedding_model=payload.embedding_model or app.embedding_model,
            enabled_tools=self._enabled_tools(tool_config),
            workflow_config=workflow_config,
            model_routes=self._safe_json_dict(app.llm_routing_json) or None,
        )
        return self.agent_service.start(agent_payload)

    def _require_identity(self, team_id: str | None, user_id: str | None) -> tuple[str, str]:
        normalized_team_id = str(team_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        if not normalized_team_id or not normalized_user_id:
            raise DomainValidationError("team_id and user_id are required.")
        return normalized_team_id, normalized_user_id

    def _resolve_space_id(self, *, team_id: str, user_id: str, space_id: str | None) -> str | None:
        if not space_id:
            return None
        return self.document_service.resolve_space_id(
            team_id=team_id,
            user_id=user_id,
            conversation_id=None,
            space_id=space_id,
        )

    def _normalize_mode(self, raw_mode: str) -> str:
        mode = raw_mode.strip().lower()
        if mode not in self._ALLOWED_MODES:
            raise DomainValidationError(
                "mode must be one of: agent_auto, workflow, claude_code, codex, pi."
            )
        return mode

    def _normalize_status(self, raw_status: str) -> str:
        status = raw_status.strip().lower()
        if status not in self._ALLOWED_STATUSES:
            raise DomainValidationError("status must be one of: draft, published, archived.")
        return status

    def _normalize_retrieval_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        config = {
            "top_k": 5,
            "include_memory": True,
            "include_library": True,
            "include_conclusions": False,
        }
        config.update(raw or {})
        config["top_k"] = max(1, min(int(config.get("top_k") or 5), 20))
        config["include_memory"] = bool(config.get("include_memory"))
        config["include_library"] = bool(config.get("include_library"))
        config["include_conclusions"] = bool(config.get("include_conclusions"))
        return config

    def _normalize_tool_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        # None means "all tools from catalog" — filtering happens at execution time.
        raw_enabled = raw.get("enabled_tools") if isinstance(raw, dict) else None
        if isinstance(raw_enabled, list):
            enabled_tools = [str(item).strip() for item in raw_enabled if str(item).strip()] or None
        else:
            enabled_tools = None
        return {
            "enabled_tools": enabled_tools,
            "dangerous_requires_confirmation": bool((raw or {}).get("dangerous_requires_confirmation", True)),
        }

    def _normalize_llm_routing(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, Any] = {}
        for role in ("planner", "fast", "vision"):
            value = raw.get(role)
            if value is None:
                result[role] = None
            elif isinstance(value, str) and value.strip():
                result[role] = value.strip()
            elif isinstance(value, dict):
                result[role] = value
            else:
                result[role] = None
        return result

    def _normalize_workflow_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        nodes = raw.get("nodes") if isinstance(raw, dict) else None
        if nodes is None:
            nodes = []
        if not isinstance(nodes, list):
            raise DomainValidationError("workflow_config.nodes must be a list.")
        return {
            "nodes": [node for node in nodes if isinstance(node, dict)],
            "max_steps": max(3, min(int((raw or {}).get("max_steps") or 5), 10)),
        }

    def _enabled_tools(self, tool_config: dict[str, Any]) -> list[str] | None:
        raw_items = tool_config.get("enabled_tools")
        if not isinstance(raw_items, list):
            return None
        items = [str(item).strip() for item in raw_items if str(item).strip()]
        return items if items else None

    def _app_snapshot(self, app: AgentApp, *, version_number: int) -> dict[str, Any]:
        return {
            "app_id": app.app_id,
            "version_number": version_number,
            "name": app.name,
            "description": app.description,
            "mode": app.mode,
            "system_prompt": app.system_prompt,
            "model": app.model,
            "embedding_model": app.embedding_model,
            "space_id": app.space_id,
            "retrieval_config": self._safe_json_dict(app.retrieval_config_json),
            "tool_config": self._safe_json_dict(app.tool_config_json),
            "workflow_config": self._safe_json_dict(app.workflow_config_json),
            "llm_routing": _sanitize_llm_routing_output(self._safe_json_dict(app.llm_routing_json)),
        }

    def _clean_optional(self, raw: str | None) -> str | None:
        value = (raw or "").strip()
        return value or None

    def _coalesce_bool(self, first: bool | None, second: object, default: bool) -> bool:
        if first is not None:
            return bool(first)
        if second is not None:
            return bool(second)
        return default

    def _to_json(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    def _safe_json_dict(self, raw: str) -> dict[str, Any]:
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
