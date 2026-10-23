"""An explicit, account-scoped notebook over the memories used by API tasks."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.chat_history import ChatHistory
from app.models.memory_card import MemoryCard
from app.schemas.companion_memory import (
    CompanionMemoryCreate, CompanionMemoryDocument, CompanionMemoryDocumentUpdate,
    CompanionMemoryItem, CompanionMemoryListResponse, CompanionMemorySource,
    CompanionMemoryUpdate, MemoryDocumentKind,
)
from app.schemas.memory import MemoryCardCreate, MemoryCardResponse, MemoryCardUpdate
from app.services.agent_service import AgentService
from app.services.memory_markdown_store import MemoryMarkdownStore
from app.services.memory_service import MemoryService


_DOCUMENTS = (
    ("self_model", "彩宝的相处方式", 6000),
    ("long_term", "长期记忆", 6000),
    ("recent_context", "近期上下文", 3000),
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class CompanionMemoryService:
    def __init__(self, *, db: Session, memory_service: MemoryService, agent_service: AgentService) -> None:
        self.db = db
        self.memory_service = memory_service
        self.agent_service = agent_service

    def _space(self, *, conversation_id: str, team_id: str, user_id: str) -> str:
        space_id = self.agent_service.document_service.resolve_space_id(
            team_id=team_id, user_id=user_id, conversation_id=conversation_id, space_id=None,
        )
        if not space_id:
            raise DomainValidationError("当前对话还没有可用的记忆空间。")
        return space_id

    def _store(self) -> MemoryMarkdownStore:
        # Dependency-created AgentService shares the exact store used by prompts.
        return self.agent_service.memory_store or MemoryMarkdownStore()

    def _document(self, *, kind: MemoryDocumentKind, team_id: str, user_id: str,
                  space_id: str) -> CompanionMemoryDocument:
        label, prompt_limit = next((label, limit) for name, label, limit in _DOCUMENTS if name == kind)
        snapshot = self._store().snapshot_document(team_id, user_id, space_id, kind)
        return CompanionMemoryDocument(
            kind=kind, label=label, content=snapshot["content"], revision=snapshot["revision"],
            enabled=get_settings().memory_markdown_enabled, prompt_limit=prompt_limit,
        )

    def _item(self, memory: MemoryCard, *, conversation_id: str, team_id: str,
              user_id: str) -> CompanionMemoryItem:
        status = memory.status
        if status == "active" and memory.expires_at and _utc(memory.expires_at) <= datetime.now(timezone.utc):
            status = "expired"
        source = None
        if memory.source_message_id:
            source = CompanionMemorySource(message_id=memory.source_message_id, available=False)
            message = self.db.get(ChatHistory, memory.source_message_id)
            if message and (message.team_id, message.user_id, message.conversation_id) == (
                team_id, user_id, conversation_id,
            ):
                source.available = True
                source.request_preview = message.request_text[:240]
                source.response_preview = message.response_text[:240]
        return CompanionMemoryItem(
            **MemoryCardResponse.model_validate(memory).model_dump(),
            scope_level=memory.scope_level, effective_status=status, source=source,
        )

    def list(self, *, conversation_id: str, team_id: str, user_id: str,
             offset: int = 0, limit: int = 50) -> CompanionMemoryListResponse:
        space_id = self._space(conversation_id=conversation_id, team_id=team_id, user_id=user_id)
        rows = list(self.db.scalars(select(MemoryCard).where(
            MemoryCard.team_id == team_id, MemoryCard.user_id == user_id,
            or_(
                (MemoryCard.scope_level == "space") & (MemoryCard.space_id == space_id),
                (MemoryCard.scope_level == "global") & MemoryCard.space_id.is_(None),
            ),
        ).order_by(MemoryCard.updated_at.desc(), MemoryCard.memory_id.desc()).offset(offset).limit(limit + 1)).all())
        return CompanionMemoryListResponse(
            conversation_id=conversation_id, space_id=space_id,
            items=[self._item(row, conversation_id=conversation_id, team_id=team_id, user_id=user_id)
                   for row in rows[:limit]],
            has_more=len(rows) > limit, next_offset=offset + min(len(rows), limit),
            documents=[self._document(kind=kind, team_id=team_id, user_id=user_id, space_id=space_id)
                       for kind, _, _ in _DOCUMENTS],
            markdown_enabled=get_settings().memory_markdown_enabled,
        )

    def create(self, payload: CompanionMemoryCreate, *, team_id: str, user_id: str) -> CompanionMemoryItem:
        space_id = self._space(conversation_id=payload.conversation_id, team_id=team_id, user_id=user_id)
        if payload.source_message_id:
            source = self.db.get(ChatHistory, payload.source_message_id)
            if source is None or (source.team_id, source.user_id, source.conversation_id) != (
                team_id, user_id, payload.conversation_id,
            ):
                raise EntityNotFoundError("来源消息不在当前对话中。")
        memory = self.memory_service.create(MemoryCardCreate(
            team_id=team_id, user_id=user_id, space_id=space_id,
            title=payload.title, content=payload.content, category=payload.category,
            source_message_id=payload.source_message_id,
        ))
        return self._item(memory, conversation_id=payload.conversation_id, team_id=team_id, user_id=user_id)

    def _card(self, *, memory_id: str, conversation_id: str, team_id: str, user_id: str) -> MemoryCard:
        space_id = self._space(conversation_id=conversation_id, team_id=team_id, user_id=user_id)
        memory = self.memory_service.ensure_access(memory_id=memory_id, team_id=team_id, user_id=user_id)
        if not ((memory.scope_level == "space" and memory.space_id == space_id)
                or (memory.scope_level == "global" and memory.space_id is None)):
            raise EntityNotFoundError("当前对话中没有这张记忆卡。")
        return memory

    def update(self, memory_id: str, payload: CompanionMemoryUpdate, *, team_id: str,
               user_id: str) -> CompanionMemoryItem:
        self._card(memory_id=memory_id, conversation_id=payload.conversation_id, team_id=team_id, user_id=user_id)
        values = payload.model_dump(exclude={"conversation_id"}, exclude_none=True)
        memory = self.memory_service.update(memory_id=memory_id, payload=MemoryCardUpdate(
            team_id=team_id, user_id=user_id, **values,
        ))
        return self._item(memory, conversation_id=payload.conversation_id, team_id=team_id, user_id=user_id)

    def delete(self, memory_id: str, *, conversation_id: str, team_id: str, user_id: str) -> None:
        self._card(memory_id=memory_id, conversation_id=conversation_id, team_id=team_id, user_id=user_id)
        self.memory_service.delete(memory_id=memory_id, team_id=team_id, user_id=user_id)

    def update_document(self, kind: MemoryDocumentKind, payload: CompanionMemoryDocumentUpdate,
                        *, team_id: str, user_id: str) -> CompanionMemoryDocument:
        space_id = self._space(conversation_id=payload.conversation_id, team_id=team_id, user_id=user_id)
        if not get_settings().memory_markdown_enabled:
            raise DomainValidationError("Markdown 记忆尚未启用；可先管理记忆卡。")
        snapshot = self._store().update_document(
            team_id, user_id, space_id, kind, content=payload.content,
            expected_revision=payload.expected_revision,
        )
        label, limit = next((label, limit) for name, label, limit in _DOCUMENTS if name == kind)
        return CompanionMemoryDocument(kind=kind, label=label, content=snapshot["content"],
                                       revision=snapshot["revision"], enabled=True, prompt_limit=limit)
