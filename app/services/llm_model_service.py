from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.llm_model_config import LLMModelConfig
from app.services.model_base_url import normalize_openai_compatible_base_url
from app.services.user_service import UserService


@dataclass(slots=True)
class RuntimeModelConfig:
    model_name: str
    base_url: str
    api_key: str


class LLMModelService:
    def __init__(self, db: Session, user_service: UserService) -> None:
        self.db = db
        self.user_service = user_service

    def list_configs(self, *, team_id: str, user_id: str) -> list[LLMModelConfig]:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)

        stmt = (
            select(LLMModelConfig)
            .where(
                LLMModelConfig.team_id == team_id,
                LLMModelConfig.user_id == user_id,
            )
            .order_by(LLMModelConfig.updated_at.desc(), LLMModelConfig.model_name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def upsert_config(
        self,
        *,
        team_id: str,
        user_id: str,
        model_name: str,
        base_url: str,
        api_key: str,
    ) -> LLMModelConfig:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)

        normalized_name = model_name.strip()
        normalized_base_url = self._normalize_base_url(base_url)
        normalized_api_key = api_key.strip()

        if not normalized_name:
            raise DomainValidationError("model_name cannot be empty.")
        if normalized_name.lower() in {"default", "none"}:
            raise DomainValidationError("'default' and 'none' are reserved and cannot be used as custom models.")
        if not normalized_api_key:
            raise DomainValidationError("api_key cannot be empty.")

        existing = self._get_by_name(team_id=team_id, user_id=user_id, model_name=normalized_name)
        now = datetime.now(timezone.utc)
        if existing is not None:
            existing.base_url = normalized_base_url
            existing.api_key = normalized_api_key
            existing.updated_at = now
            self.db.commit()
            self.db.refresh(existing)
            return existing

        item = LLMModelConfig(
            config_id=str(uuid4()),
            team_id=team_id,
            user_id=user_id,
            model_name=normalized_name,
            base_url=normalized_base_url,
            api_key=normalized_api_key,
            created_at=now,
            updated_at=now,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def delete_config(self, *, team_id: str, user_id: str, model_name: str) -> None:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)

        item = self._get_by_name(team_id=team_id, user_id=user_id, model_name=model_name.strip())
        if item is None:
            raise EntityNotFoundError(f"Model '{model_name}' is not configured for this account.")

        self.db.delete(item)
        self.db.commit()

    def resolve_runtime_config(
        self,
        *,
        team_id: str,
        user_id: str,
        model_name: str | None,
    ) -> RuntimeModelConfig | None:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)

        if model_name is None:
            return None

        normalized_name = model_name.strip()
        if not normalized_name or normalized_name.lower() == "default":
            return None

        item = self._get_by_name(team_id=team_id, user_id=user_id, model_name=normalized_name)
        if item is None:
            raise EntityNotFoundError(f"Model '{normalized_name}' is not configured for this account.")

        return RuntimeModelConfig(
            model_name=item.model_name,
            base_url=item.base_url,
            api_key=item.api_key,
        )

    @staticmethod
    def mask_api_key(api_key: str) -> str:
        normalized = api_key.strip()
        if len(normalized) <= 8:
            return "*" * len(normalized)
        return f"{normalized[:4]}...{normalized[-4:]}"

    def _get_by_name(self, *, team_id: str, user_id: str, model_name: str) -> LLMModelConfig | None:
        stmt = select(LLMModelConfig).where(
            LLMModelConfig.team_id == team_id,
            LLMModelConfig.user_id == user_id,
            LLMModelConfig.model_name == model_name,
        )
        return self.db.scalars(stmt).first()

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        return normalize_openai_compatible_base_url(raw)
