from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.chat_history import ChatHistory
from app.models.conversation import Conversation
from app.models.team import Team


class ChatHistoryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record_message(
        self,
        *,
        team_id: str,
        user_id: str,
        conversation_id: str | None,
        channel: str,
        request_text: str,
        response_text: str,
        request_payload: dict[str, object],
        response_payload: dict[str, object],
    ) -> ChatHistory:
        normalized_channel = channel.strip().lower()
        if normalized_channel not in {"echo", "ask", "action", "agent", "qqbot"}:
            raise DomainValidationError("channel must be one of: echo, ask, action, agent, qqbot.")

        space_id: str | None = None
        if conversation_id is not None:
            conversation = self.db.get(Conversation, conversation_id)
            if conversation is None:
                raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
            if conversation.team_id != team_id or conversation.user_id != user_id:
                raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
            space_id = conversation.space_id

        message = ChatHistory(
            message_id=str(uuid4()),
            team_id=team_id,
            user_id=user_id,
            conversation_id=conversation_id,
            space_id=space_id,
            channel=normalized_channel,
            request_text=request_text,
            response_text=response_text,
            request_payload_json=json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")),
            response_payload_json=json.dumps(response_payload, ensure_ascii=False, separators=(",", ":")),
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        return message

    def list_history(
        self,
        *,
        team_id: str,
        user_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ChatHistory]:
        self._ensure_team_exists(team_id)

        if limit < 1 or limit > 200:
            raise DomainValidationError("limit must be between 1 and 200.")

        stmt = select(ChatHistory).where(ChatHistory.team_id == team_id)
        if user_id is not None:
            stmt = stmt.where(ChatHistory.user_id == user_id)
        if conversation_id is not None:
            self._ensure_conversation_access(
                team_id=team_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            stmt = stmt.where(ChatHistory.conversation_id == conversation_id)

        stmt = stmt.order_by(ChatHistory.created_at.desc(), ChatHistory.message_id.desc()).limit(limit)
        return list(self.db.scalars(stmt).all())

    def list_messages_for_context(
        self,
        *,
        team_id: str,
        user_id: str,
        conversation_id: str | None,
        limit: int,
        before_message_id: str | None = None,
    ) -> list[ChatHistory]:
        if conversation_id is None or limit < 1:
            return []

        self._ensure_conversation_access(
            team_id=team_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

        stmt = select(ChatHistory).where(
            ChatHistory.team_id == team_id,
            ChatHistory.user_id == user_id,
            ChatHistory.conversation_id == conversation_id,
        )

        if before_message_id is not None:
            boundary_message = self.get_message(
                message_id=before_message_id,
                team_id=team_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            stmt = stmt.where(ChatHistory.created_at < boundary_message.created_at)

        stmt = stmt.order_by(ChatHistory.created_at.desc(), ChatHistory.message_id.desc()).limit(limit)
        items = list(self.db.scalars(stmt).all())
        items.reverse()
        return items

    def get_message(
        self,
        *,
        message_id: str,
        team_id: str,
        user_id: str,
        conversation_id: str | None = None,
    ) -> ChatHistory:
        message = self.db.get(ChatHistory, message_id)
        if message is None:
            raise EntityNotFoundError(f"Message '{message_id}' not found.")

        if message.team_id != team_id or message.user_id != user_id:
            raise EntityNotFoundError(f"Message '{message_id}' not found.")

        if conversation_id is not None and message.conversation_id != conversation_id:
            raise EntityNotFoundError(f"Message '{message_id}' not found.")

        return message

    def delete_message(
        self,
        *,
        message_id: str,
        team_id: str,
        user_id: str,
        conversation_id: str | None = None,
    ) -> None:
        self.get_message(
            message_id=message_id,
            team_id=team_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

        self.db.execute(delete(ChatHistory).where(ChatHistory.message_id == message_id))
        self.db.commit()

    def update_message(
        self,
        *,
        message_id: str,
        team_id: str,
        user_id: str,
        request_text: str,
        response_text: str,
        request_payload: dict[str, object],
        response_payload: dict[str, object],
    ) -> ChatHistory:
        message = self.get_message(
            message_id=message_id,
            team_id=team_id,
            user_id=user_id,
        )

        message.request_text = request_text
        message.response_text = response_text
        message.request_payload_json = json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        message.response_payload_json = json.dumps(
            response_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.db.commit()
        self.db.refresh(message)
        return message

    def get_latest_message(
        self,
        *,
        team_id: str,
        user_id: str,
        conversation_id: str | None,
    ) -> ChatHistory | None:
        self._ensure_team_exists(team_id)

        stmt = select(ChatHistory).where(
            ChatHistory.team_id == team_id,
            ChatHistory.user_id == user_id,
        )
        if conversation_id is None:
            stmt = stmt.where(ChatHistory.conversation_id.is_(None))
        else:
            self._ensure_conversation_access(
                team_id=team_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            stmt = stmt.where(ChatHistory.conversation_id == conversation_id)

        stmt = stmt.order_by(ChatHistory.created_at.desc(), ChatHistory.message_id.desc()).limit(1)
        return self.db.scalars(stmt).first()

    def ensure_latest_message(
        self,
        *,
        message_id: str,
        team_id: str,
        user_id: str,
    ) -> ChatHistory:
        message = self.get_message(
            message_id=message_id,
            team_id=team_id,
            user_id=user_id,
        )
        latest_message = self.get_latest_message(
            team_id=team_id,
            user_id=user_id,
            conversation_id=message.conversation_id,
        )
        if latest_message is None or latest_message.message_id != message.message_id:
            raise DomainValidationError("Only the latest message in a conversation can be edited or regenerated.")
        return message

    def _ensure_team_exists(self, team_id: str) -> None:
        team = self.db.get(Team, team_id)
        if team is None:
            raise EntityNotFoundError(f"Team '{team_id}' does not exist.")

    def _ensure_conversation_access(
        self,
        *,
        team_id: str,
        user_id: str | None,
        conversation_id: str,
    ) -> Conversation:
        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
        if conversation.team_id != team_id:
            raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
        if user_id is not None and conversation.user_id != user_id:
            raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
        return conversation
