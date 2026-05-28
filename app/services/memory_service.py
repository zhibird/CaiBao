from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.chat_history import ChatHistory
from app.models.memory_card import MemoryCard
from app.models.memory_card_embedding import MemoryCardEmbedding
from app.schemas.memory import MemoryCardCreate, MemoryCardUpdate
from app.services.embedding_model_service import EmbeddingModelService
from app.services.embedding_service import EmbeddingRuntimeConfig, EmbeddingService
from app.services.space_service import SpaceService
from app.services.user_service import UserService


class MemoryService:
    _ALLOWED_STATUSES = {"active", "disabled", "expired"}

    def __init__(
        self,
        db: Session,
        user_service: UserService,
        space_service: SpaceService,
        embedding_service: EmbeddingService,
        embedding_model_service: EmbeddingModelService,
    ) -> None:
        self.db = db
        self.user_service = user_service
        self.space_service = space_service
        self.embedding_service = embedding_service
        self.embedding_model_service = embedding_model_service

    def create(self, payload: MemoryCardCreate):
        self.space_service.ensure_access(
            space_id=payload.space_id,
            team_id=payload.team_id,
            user_id=payload.user_id,
        )
        self._validate_source_message(
            source_message_id=payload.source_message_id,
            team_id=payload.team_id,
            user_id=payload.user_id,
        )

        status = self._normalize_status(payload.status)
        now = datetime.now(timezone.utc)
        memory_model = self._memory_model()
        memory = memory_model(
            memory_id=str(uuid4()),
            team_id=payload.team_id,
            space_id=payload.space_id,
            user_id=payload.user_id,
            scope_level="space",
            category=payload.category.strip(),
            title=payload.title.strip(),
            content=payload.content.strip(),
            summary=(payload.summary or "").strip() or None,
            weight=float(payload.weight),
            confidence=float(payload.confidence),
            status=status,
            source_message_id=payload.source_message_id,
            expires_at=payload.expires_at,
            created_at=now,
            updated_at=now,
        )
        self.db.add(memory)
        self.db.commit()
        self.db.refresh(memory)
        self._upsert_embedding(
            memory=memory,
            runtime=self._resolve_runtime(
                team_id=payload.team_id,
                user_id=payload.user_id,
                embedding_model=None,
            ),
        )
        return memory

    def list(
        self,
        *,
        team_id: str,
        user_id: str,
        space_id: str,
        status: str | None,
        limit: int,
    ) -> list:
        self.space_service.ensure_access(space_id=space_id, team_id=team_id, user_id=user_id)
        if limit < 1 or limit > 200:
            raise DomainValidationError("limit must be between 1 and 200.")

        memory_model = self._memory_model()
        stmt = select(memory_model).where(
            memory_model.team_id == team_id,
            memory_model.space_id == space_id,
            memory_model.user_id == user_id,
        )
        if status is not None:
            stmt = stmt.where(memory_model.status == self._normalize_status(status))

        stmt = stmt.order_by(memory_model.updated_at.desc(), memory_model.memory_id.desc()).limit(limit)
        return list(self.db.scalars(stmt).all())

    def update(self, *, memory_id: str, payload: MemoryCardUpdate):
        memory = self.ensure_access(memory_id=memory_id, team_id=payload.team_id, user_id=payload.user_id)
        content_changed = False

        if payload.category is not None:
            memory.category = payload.category.strip()
        if payload.title is not None:
            memory.title = payload.title.strip()
        if payload.content is not None:
            memory.content = payload.content.strip()
            content_changed = True
        if payload.summary is not None:
            memory.summary = payload.summary.strip() or None
        if payload.weight is not None:
            memory.weight = float(payload.weight)
        if payload.confidence is not None:
            memory.confidence = float(payload.confidence)
        if payload.status is not None:
            memory.status = self._normalize_status(payload.status)
        if payload.expires_at is not None:
            memory.expires_at = payload.expires_at

        memory.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(memory)
        if content_changed:
            self._upsert_embedding(
                memory=memory,
                runtime=self._resolve_runtime(
                    team_id=payload.team_id,
                    user_id=payload.user_id,
                    embedding_model=None,
                ),
            )
        return memory

    def delete(self, *, memory_id: str, team_id: str, user_id: str) -> None:
        memory = self.ensure_access(memory_id=memory_id, team_id=team_id, user_id=user_id)
        self.db.execute(
            delete(MemoryCardEmbedding).where(
                MemoryCardEmbedding.memory_id == memory.memory_id,
                MemoryCardEmbedding.team_id == team_id,
            )
        )
        self.db.delete(memory)
        self.db.commit()

    def search_cards_for_chat(
        self,
        *,
        team_id: str,
        user_id: str,
        space_id: str,
        query: str,
        top_k: int = 3,
        embedding_model: str | None = None,
    ) -> list[dict[str, object]]:
        self.space_service.ensure_access(space_id=space_id, team_id=team_id, user_id=user_id)
        normalized_query = query.strip()
        if not normalized_query:
            return []

        runtime = self._resolve_runtime(
            team_id=team_id,
            user_id=user_id,
            embedding_model=embedding_model,
        )
        query_vec = self.embedding_service.embed_text(normalized_query, runtime=runtime)
        query_dim = len(query_vec)
        if query_dim <= 0:
            return []

        now = datetime.now(timezone.utc)
        stmt = (
            select(MemoryCardEmbedding, MemoryCard)
            .join(MemoryCard, MemoryCardEmbedding.memory_id == MemoryCard.memory_id)
            .where(
                MemoryCardEmbedding.team_id == team_id,
                MemoryCard.user_id == user_id,
                MemoryCard.status == "active",
                or_(
                    MemoryCard.scope_level == "global",
                    MemoryCard.space_id == space_id,
                ),
            )
        )
        rows = self.db.execute(stmt).all()
        hits: list[dict[str, object]] = []
        for embedding, memory in rows:
            expires_at = memory.expires_at
            if expires_at is not None and expires_at <= now:
                continue

            try:
                vector = json.loads(embedding.vector_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(vector, list):
                continue
            numeric_vector: list[float] = []
            valid = True
            for value in vector:
                if not isinstance(value, (int, float)):
                    valid = False
                    break
                numeric_vector.append(float(value))
            if not valid or len(numeric_vector) != query_dim:
                continue

            score = self.embedding_service.cosine_similarity(query_vec, numeric_vector) * float(memory.weight)
            hits.append(
                {
                    "memory_id": memory.memory_id,
                    "title": memory.title,
                    "content": memory.content,
                    "score": float(score),
                }
            )

        hits.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return hits[: max(1, min(top_k, 10))]

    def ensure_access(self, *, memory_id: str, team_id: str, user_id: str):
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        memory_model = self._memory_model()
        memory = self.db.get(memory_model, memory_id)
        if memory is None:
            raise EntityNotFoundError(f"Memory card '{memory_id}' not found.")
        if memory.team_id != team_id or memory.user_id != user_id:
            raise EntityNotFoundError(f"Memory card '{memory_id}' not found.")
        self.space_service.ensure_access(
            space_id=memory.space_id,
            team_id=team_id,
            user_id=user_id,
        )
        return memory

    def _normalize_status(self, raw_status: str) -> str:
        normalized_status = raw_status.strip().lower()
        if normalized_status not in self._ALLOWED_STATUSES:
            raise DomainValidationError("status must be one of: active, disabled, expired.")
        return normalized_status

    def _validate_source_message(
        self,
        *,
        source_message_id: str | None,
        team_id: str,
        user_id: str,
    ) -> None:
        normalized_id = (source_message_id or "").strip()
        if not normalized_id:
            return
        message = self.db.get(ChatHistory, normalized_id)
        if message is None:
            raise EntityNotFoundError(f"Message '{normalized_id}' not found.")
        if message.team_id != team_id or message.user_id != user_id:
            raise EntityNotFoundError(f"Message '{normalized_id}' not found.")

    def _upsert_embedding(
        self,
        *,
        memory: MemoryCard,
        runtime: EmbeddingRuntimeConfig | None,
    ) -> None:
        vector = self.embedding_service.embed_text(memory.content, runtime=runtime)
        payload = json.dumps(vector, separators=(",", ":"))
        current_model = self.embedding_service.model_name
        existing = self.db.scalar(
            select(MemoryCardEmbedding).where(MemoryCardEmbedding.memory_id == memory.memory_id)
        )
        if existing is None:
            existing = MemoryCardEmbedding(
                embedding_id=str(uuid4()),
                memory_id=memory.memory_id,
                team_id=memory.team_id,
                space_id=memory.space_id,
                embedding_model=current_model,
                vector_json=payload,
                vector_dim=len(vector),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            self.db.add(existing)
        else:
            existing.space_id = memory.space_id
            existing.embedding_model = current_model
            existing.vector_json = payload
            existing.vector_dim = len(vector)
            existing.updated_at = datetime.now(timezone.utc)
            self.db.add(existing)
        self.db.commit()

    def _resolve_runtime(
        self,
        *,
        team_id: str,
        user_id: str,
        embedding_model: str | None,
    ) -> EmbeddingRuntimeConfig | None:
        normalized = (embedding_model or "").strip()
        if not normalized or normalized.lower() == "default":
            return None
        if normalized.lower() in {"mock", "none"}:
            return EmbeddingRuntimeConfig.mock_default()
        return self.embedding_model_service.resolve_runtime_config(
            team_id=team_id,
            user_id=user_id,
            model_name=normalized,
        )

    def create_from_consolidation(
        self,
        *,
        team_id: str,
        user_id: str,
        space_id: str | None,
        history_entries: list[dict],
        source_ref: str,
    ) -> list[str]:
        """Idempotent upsert of consolidation history entries as vector cards.

        Returns list of created/updated memory_id strings.
        """
        created_ids: list[str] = []
        for i, entry in enumerate(history_entries):
            summary = str(entry.get("summary", ""))[:2000]
            if not summary.strip():
                continue
            title = summary[:80]
            entry_ref = f"{source_ref}:{i}"

            # Idempotency: skip if same per-entry source_ref already exists
            existing = self.db.scalar(
                select(MemoryCard).where(
                    MemoryCard.team_id == team_id,
                    MemoryCard.user_id == user_id,
                    MemoryCard.space_id == space_id,
                    MemoryCard.source_ref == entry_ref,
                    MemoryCard.memory_type == "history",
                )
            )
            if existing is not None:
                created_ids.append(existing.memory_id)
                continue

            now = datetime.now(timezone.utc)
            card = MemoryCard(
                memory_id=str(uuid4()),
                team_id=team_id,
                space_id=space_id,
                user_id=user_id,
                scope_level="space" if space_id else "global",
                category="history",
                title=title,
                content=summary,
                summary=summary[:500],
                weight=0.7,
                confidence=0.8,
                status="active",
                source_ref=entry_ref,
                memory_type="history",
                created_at=now,
                updated_at=now,
            )
            self.db.add(card)
            self.db.commit()
            self.db.refresh(card)

            # Generate embedding
            try:
                self._upsert_embedding(memory=card, runtime=None)
            except Exception:
                pass  # embedding failure is non-fatal

            created_ids.append(card.memory_id)

        return created_ids

    def _memory_model(self):
        return MemoryCard
