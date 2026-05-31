from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.llm_model_service import LLMModelService


@dataclass(frozen=True, slots=True)
class ModelRoute:
    model_name: str
    base_url: str
    api_key: str | None = None
    source: str = "default"
    capabilities: dict[str, Any] = field(default_factory=dict)


class LLMRouter:
    """Resolves which LLM to use for planner / fast / vision roles.

    Priority per role:
      1.  AgentRunRequest.model_routes (explicit overrides)
      2.  AgentApp.llm_routing_json (per-app defaults)
      3.  AgentRunRequest.model + AgentApp.model (single-model legacy)
      4.  Global settings (LLM_PROVIDER, LLM_MODEL, etc.)
    """

    def __init__(
        self,
        db: Session,
        user_id: str,
        team_id: str,
        llm_model_service: LLMModelService,
    ) -> None:
        self.db = db
        self.user_id = user_id
        self.team_id = team_id
        self.llm_model_service = llm_model_service
        self.settings = get_settings()

    def resolve(
        self,
        *,
        explicit_model: str | None,
        app_llm_routing: dict[str, Any] | None = None,
        explicit_routes: dict[str, Any] | None = None,
    ) -> dict[str, ModelRoute]:
        """Return dict with 'planner', 'fast', 'vision' ModelRoute keys."""
        app_routing = app_llm_routing or {}
        req_routes = explicit_routes or {}

        planner = self._resolve_role(
            role_key="planner",
            explicit_route=req_routes.get("planner"),
            app_route=app_routing.get("planner"),
            fallback_model=explicit_model,
        )
        fast = self._resolve_role(
            role_key="fast",
            explicit_route=req_routes.get("fast"),
            app_route=app_routing.get("fast"),
            fallback_model=self.settings.llm_fast_model or planner.model_name,
        )
        vision = self._resolve_role(
            role_key="vision",
            explicit_route=req_routes.get("vision"),
            app_route=app_routing.get("vision"),
            fallback_model=self.settings.llm_vl_model or planner.model_name,
        )

        return {"planner": planner, "fast": fast, "vision": vision}

    def _resolve_role(
        self,
        *,
        role_key: str,
        explicit_route: Any,
        app_route: Any,
        fallback_model: str | None,
    ) -> ModelRoute:
        # 1. Explicit per-request route
        route = self._parse_route_value(explicit_route, role_key=role_key)
        if route is not None:
            return route

        # 2. Per-app routing config
        route = self._parse_route_value(app_route, role_key=role_key)
        if route is not None:
            return route

        # 3. Fallback to the given model name or global default
        if fallback_model:
            return self._build_default_route(fallback_model, role_key=role_key)
        return self._global_route(role_key=role_key)

    def _parse_route_value(self, raw: Any, *, role_key: str) -> ModelRoute | None:
        if raw is None:
            return None
        if isinstance(raw, str) and raw.strip():
            return self._build_default_route(raw.strip(), role_key=role_key)
        if isinstance(raw, dict) and raw.get("model_name"):
            return self._build_route_from_dict(raw)
        return None

    def _build_default_route(self, model_name: str, *, role_key: str = "planner") -> ModelRoute:
        normalized = model_name.strip().lower()
        if normalized in {"default", "none", ""}:
            return self._global_route(role_key=role_key)

        # Look up user-configured model
        from app.core.exceptions import EntityNotFoundError
        try:
            runtime = self.llm_model_service.resolve_runtime_config(
                team_id=self.team_id,
                user_id=self.user_id,
                model_name=model_name,
            )
        except EntityNotFoundError:
            runtime = None
        if runtime is not None:
            return ModelRoute(
                model_name=runtime.model_name,
                base_url=runtime.base_url,
                api_key=runtime.api_key,
                source="user_config",
            )

        # Use name as-is with global defaults (empty base_url/api_key
        # so _resolve_runtime falls through to global settings when no
        # role-specific TOML profile is configured).
        role_base_url, role_api_key, source = self._role_runtime_defaults(role_key)
        return ModelRoute(
            model_name=model_name,
            base_url=role_base_url,
            api_key=role_api_key,
            source=source,
            capabilities={"supports_tools": True},
        )

    def _build_route_from_dict(self, d: dict[str, Any]) -> ModelRoute:
        model_name = str(d.get("model_name", "")).strip()
        base_url = str(d.get("base_url", "")).strip() or self.settings.llm_base_url
        api_key = str(d.get("api_key", "")).strip() or self.settings.llm_api_key
        capabilities = d.get("capabilities", {})
        if isinstance(capabilities, dict):
            capabilities = copy.deepcopy(capabilities)
        else:
            capabilities = {}
        return ModelRoute(
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
            source="route_config",
            capabilities=capabilities,
        )

    def _global_route(self, *, role_key: str = "planner") -> ModelRoute:
        if role_key == "fast" and self.settings.llm_fast_model.strip():
            base_url, api_key, _ = self._role_runtime_defaults("fast")
            return ModelRoute(
                model_name=self.settings.llm_fast_model,
                base_url=base_url,
                api_key=api_key,
                source="global_fast_settings",
                capabilities={"supports_tools": True},
            )
        if role_key == "vision" and self.settings.llm_vl_model.strip():
            base_url, api_key, _ = self._role_runtime_defaults("vision")
            return ModelRoute(
                model_name=self.settings.llm_vl_model,
                base_url=base_url,
                api_key=api_key,
                source="global_vision_settings",
                capabilities={"supports_vision": True},
            )
        base_url, api_key = self._runtime_pair(self.settings.llm_base_url, self.settings.llm_api_key)
        return ModelRoute(
            model_name=self.settings.llm_model,
            base_url=base_url,
            api_key=api_key,
            source="global_settings",
            capabilities={"supports_tools": True},
        )

    def _role_runtime_defaults(self, role_key: str) -> tuple[str, str | None, str]:
        if role_key == "fast":
            base_url, api_key = self._runtime_pair(
                self.settings.llm_fast_base_url or self.settings.llm_base_url,
                self.settings.llm_fast_api_key or self.settings.llm_api_key,
            )
            return (
                base_url,
                api_key,
                "direct_name_fast_settings" if self.settings.llm_fast_model.strip() else "direct_name",
            )
        if role_key == "vision":
            base_url, api_key = self._runtime_pair(
                self.settings.llm_vl_base_url or self.settings.llm_base_url,
                self.settings.llm_vl_api_key or self.settings.llm_api_key,
            )
            return (
                base_url,
                api_key,
                "direct_name_vision_settings" if self.settings.llm_vl_model.strip() else "direct_name",
            )
        return "", "", "direct_name"

    @staticmethod
    def _runtime_pair(base_url: str | None, api_key: str | None) -> tuple[str, str]:
        normalized_base_url = (base_url or "").strip()
        normalized_api_key = (api_key or "").strip()
        if not normalized_base_url or not normalized_api_key:
            return "", ""
        return normalized_base_url, normalized_api_key

    @staticmethod
    def routes_snapshot(routes: dict[str, ModelRoute]) -> dict[str, Any]:
        """Return a JSON-safe snapshot for persistence in model_routes_json."""
        snapshot: dict[str, Any] = {}
        for role, route in routes.items():
            snapshot[role] = {
                "model_name": route.model_name,
                "base_url": route.base_url,
                "api_key": LLMModelService.mask_api_key(route.api_key) if route.api_key else None,
                "source": route.source,
                "capabilities": copy.deepcopy(route.capabilities),
            }
        return snapshot
