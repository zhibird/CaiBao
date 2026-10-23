"""User-directed memory controls for the companion, without model inference."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.memory import MemoryCardResponse


MemoryDocumentKind = Literal["self_model", "long_term", "recent_context"]


class CompanionMemorySource(BaseModel):
    message_id: str
    available: bool
    request_preview: str = ""
    response_preview: str = ""


class CompanionMemoryItem(MemoryCardResponse):
    scope_level: Literal["space", "global"]
    effective_status: Literal["active", "disabled", "expired"]
    source: CompanionMemorySource | None = None

    @field_validator("created_at", "updated_at", "expires_at")
    @classmethod
    def _utc_dates(cls, value: datetime | None) -> datetime | None:
        # SQLite stores UTC without its offset; send an explicit timezone so
        # browser date formatting cannot interpret an expiry as local wall time.
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class CompanionMemoryDocument(BaseModel):
    kind: MemoryDocumentKind
    label: str
    content: str
    revision: str
    enabled: bool
    prompt_limit: int | None = None


class CompanionMemoryListResponse(BaseModel):
    conversation_id: str
    space_id: str
    items: list[CompanionMemoryItem]
    has_more: bool
    next_offset: int
    documents: list[CompanionMemoryDocument]
    markdown_enabled: bool
    automatic_memory: Literal[False] = False


class _MemoryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=36)

    @field_validator("title", "content", "category", check_fields=False)
    @classmethod
    def _nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class CompanionMemoryCreate(_MemoryEdit):
    title: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="preference", min_length=1, max_length=32)
    source_message_id: str | None = Field(default=None, min_length=1, max_length=36)


class CompanionMemoryUpdate(_MemoryEdit):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    category: str | None = Field(default=None, min_length=1, max_length=32)
    status: Literal["active", "disabled", "expired"] | None = None


class CompanionMemoryDocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=36)
    content: str = Field(max_length=32000)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
