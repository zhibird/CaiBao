from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.conclusion import Conclusion
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.incident import Incident
from app.models.memory_card import MemoryCard
from app.models.project_space import ProjectSpace
from app.schemas.memory import MemoryCardCreate
from app.schemas.tooling import ToolCallContext
from app.services.tool_catalog_service import ToolCatalogService
from app.services.tool_registry import ToolDefinition, get_tool_definition, list_tool_definitions
from app.services.tool_safety import ToolSafetyService


class ToolService:
    """Tool executor for builtin + generic + MCP tools. Dispatches via ToolCatalogService."""

    _BUILTIN_GENERIC_HANDLERS: dict[str, str] = {
        "search_knowledge": "_search_knowledge",
        "list_recent_documents": "_list_recent_documents",
        "create_incident": "_create_incident",
        "create_memory_card": "_create_memory_card",
        "forget_memory": "_forget_memory",
        "recall_memory": "_recall_memory",
        "message_lookup": "_message_lookup",
        "promote_to_conclusion": "_promote_to_conclusion",
        "generate_incident_report": "_generate_incident_report",
        "web_fetch": "_delegate_generic",
        "web_search": "_delegate_generic",
        "bilibili_latest_videos": "_delegate_generic",
        "list_dir": "_delegate_generic",
        "read_file": "_delegate_generic",
        "write_file": "_delegate_generic",
        "edit_file": "_delegate_generic",
        "shell_exec": "_delegate_generic",
        "shell_status": "_delegate_generic",
        "shell_kill": "_delegate_generic",
    }

    def __init__(
        self,
        db: Session,
        catalog: ToolCatalogService,
        safety: ToolSafetyService,
        plugin_manager=None,
        memory_service=None,
    ) -> None:
        self.db = db
        self.catalog = catalog
        self.safety = safety
        self.plugin_manager = plugin_manager
        self._memory_service = memory_service
        self._generic_handlers: dict[str, object] = {}

    def register_generic_handler(self, name: str, handler: object) -> None:
        self._generic_handlers[name] = handler

    def list_tools(self) -> list[ToolDefinition]:
        return self.catalog.list_definitions()

    def get_tool_definition(self, action: str) -> ToolDefinition | None:
        return self.catalog.get_definition(action)

    def execute(
        self,
        *,
        team_id: str,
        user_id: str,
        action: str,
        arguments: dict[str, object],
        run_id: str | None = None,
        dry_run: bool = False,
        confirmed: bool = False,
    ) -> dict[str, object]:
        action_name = action.strip().lower()
        definition = self.catalog.get_definition(action_name)
        if definition is None:
            available = ", ".join(d.name for d in self.catalog.list_definitions())
            raise DomainValidationError(f"Unsupported action '{action_name}'. Available: {available}.")

        # Safety preflight
        ctx = ToolCallContext(
            run_id=run_id, team_id=team_id, user_id=user_id,
            tool_name=action_name, arguments=arguments,
            source=definition.source, dry_run=dry_run,
        )
        preflight = self.safety.preflight(ctx, definition)
        if not preflight.allowed:
            raise DomainValidationError(preflight.reason or "Tool call blocked by safety checks.")

        # Dangerous tools require explicit confirmation (or dry_run)
        if preflight.requires_confirmation and not confirmed and not dry_run:
            raise DomainValidationError(
                f"Tool '{action_name}' requires explicit confirmation. "
                "Set confirmed=True or use the Agent confirm endpoint."
            )

        effective_args = preflight.normalized_arguments
        ctx.arguments = effective_args  # align signature with normalized args for loop guard

        # Dry-run: run all safety checks but DO NOT execute any handler
        if dry_run:
            self.safety.record_call(ctx)
            return {
                "tool_name": action_name,
                "arguments": effective_args,
                "dry_run": True,
                "would_require_confirmation": preflight.requires_confirmation,
                "would_be_dangerous": preflight.dangerous,
            }

        # Plugin tool pre-hooks: allow plugins to modify/deny arguments
        if self.plugin_manager is not None:
            effective_args = self.plugin_manager.check_tool_pre(
                action_name, effective_args, definition,
            )

        # Dispatch
        if definition.source == "mcp":
            return self._dispatch_mcp(
                definition=definition, arguments=effective_args,
                team_id=team_id, user_id=user_id,
            )

        handler_key = self._BUILTIN_GENERIC_HANDLERS.get(action_name)
        if handler_key is None:
            raise DomainValidationError(f"Unsupported action '{action_name}'.")

        if handler_key == "_delegate_generic":
            handler = self._generic_handlers.get(action_name)
            if handler is None:
                raise DomainValidationError(f"No handler registered for generic tool '{action_name}'.")
            result = handler(team_id=team_id, user_id=user_id, arguments=effective_args)
        else:
            method = getattr(self, handler_key)
            result = method(team_id=team_id, user_id=user_id, arguments=effective_args)

        # Safety postflight
        result = self.safety.postflight_output(result, source=definition.source)

        # Record call for loop guard
        self.safety.record_call(ctx)
        return result

    def _dispatch_mcp(
        self,
        *,
        definition: ToolDefinition,
        arguments: dict[str, object],
        team_id: str,
        user_id: str,
    ) -> dict[str, object]:
        """Dispatch to MCP tool handler. Registered by MCP manager."""
        handler = self._generic_handlers.get(definition.name)
        if handler is None:
            raise DomainValidationError(f"MCP tool '{definition.name}' has no active handler.")
        return handler(team_id=team_id, user_id=user_id, arguments=arguments)

    def _search_knowledge(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise DomainValidationError("search_knowledge requires a non-empty 'query'.")
        limit = self._parse_limit(arguments.get("limit", 5))
        space_id = self._optional_space_id(team_id=team_id, user_id=user_id, arguments=arguments)

        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_query = f"%{escaped}%"
        stmt = self._user_documents(team_id=team_id, user_id=user_id).where(
            Document.retrieval_enabled.is_(True),
            Document.status.in_(["ready", "uploaded"]),
            or_(
                Document.source_name.ilike(like_query, escape="\\"),
                Document.content.ilike(like_query, escape="\\"),
            ),
        )
        if space_id is not None:
            stmt = stmt.where(Document.space_id == space_id)
        stmt = stmt.order_by(Document.updated_at.desc(), Document.created_at.desc()).limit(limit)
        documents = list(self.db.scalars(stmt).all())

        return {
            "tool_name": "search_knowledge",
            "message": f"Found {len(documents)} matching knowledge documents.",
            "matches": [
                {
                    "document_id": item.document_id,
                    "source_name": item.source_name,
                    "space_id": item.space_id,
                    "snippet": self._snippet(item.content, query=query),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in documents
            ],
        }

    def _create_incident(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        raw_title = str(arguments.get("title", "")).strip()
        if not raw_title:
            raise DomainValidationError("create_incident requires a non-empty 'title'.")
        if len(raw_title) > 255:
            raise DomainValidationError("title must be at most 255 characters.")

        severity = str(arguments.get("severity", "P2")).upper().strip()
        if severity not in {"P1", "P2", "P3"}:
            raise DomainValidationError("severity must be one of: P1, P2, P3.")

        incident = Incident(
            incident_id=str(uuid4()),
            team_id=team_id,
            created_by_user_id=user_id,
            title=raw_title,
            severity=severity,
            status="open",
        )
        self.db.add(incident)
        self.db.commit()
        self.db.refresh(incident)

        return {
            "tool_name": "create_incident",
            "message": "Incident created successfully.",
            "incident": {
                "incident_id": incident.incident_id,
                "team_id": incident.team_id,
                "created_by_user_id": incident.created_by_user_id,
                "title": incident.title,
                "severity": incident.severity,
                "status": incident.status,
                "created_at": incident.created_at.isoformat(),
            },
        }

    def _list_recent_documents(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        limit = self._parse_limit(arguments.get("limit", 5))
        space_id = self._optional_space_id(team_id=team_id, user_id=user_id, arguments=arguments)

        stmt = self._user_documents(team_id=team_id, user_id=user_id)
        if space_id is not None:
            stmt = stmt.where(Document.space_id == space_id)
        stmt = stmt.order_by(Document.created_at.desc()).limit(limit)
        documents = list(self.db.scalars(stmt).all())

        return {
            "tool_name": "list_recent_documents",
            "message": f"Fetched {len(documents)} documents.",
            "documents": [
                {
                    "document_id": item.document_id,
                    "source_name": item.source_name,
                    "content_type": item.content_type,
                    "space_id": item.space_id,
                    "created_at": item.created_at.isoformat(),
                }
                for item in documents
            ],
        }

    def _create_memory_card(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        space_id = self._required_space_id(team_id=team_id, user_id=user_id, arguments=arguments)
        title = str(arguments.get("title", "")).strip()
        content = str(arguments.get("content", "")).strip()
        category = str(arguments.get("category", "agent")).strip() or "agent"
        if not title or not content:
            raise DomainValidationError("create_memory_card requires non-empty 'title' and 'content'.")
        if len(title) > 128:
            raise DomainValidationError("title must be at most 128 characters.")
        if len(content) > 4000:
            raise DomainValidationError("content must be at most 4000 characters.")

        # Use the same writer as the notebook: an approved fact must receive
        # its retrieval index too, so later chat can recall it without tools.
        memory = self._memory_writer().create(MemoryCardCreate(
            team_id=team_id,
            space_id=space_id,
            user_id=user_id,
            category=category[:32],
            title=title,
            content=content,
            summary=str(arguments.get("summary", "")).strip()[:2000] or None,
            weight=0.8,
            confidence=0.9,
            status="active",
            source_message_id=None,
            expires_at=None,
        ))
        return {
            "tool_name": "create_memory_card",
            "message": "Memory card created successfully.",
            "memory": {
                "memory_id": memory.memory_id,
                "space_id": memory.space_id,
                "title": memory.title,
                "category": memory.category,
                "status": memory.status,
            },
        }

    def _memory_writer(self):
        if self._memory_service is None:
            # Keep standalone ToolService users on the same write path as the
            # dependency-injected server, using this service's own DB session.
            from app.core.config import get_settings
            from app.services.embedding_model_service import EmbeddingModelService
            from app.services.embedding_service import EmbeddingService
            from app.services.memory_service import MemoryService
            from app.services.space_service import SpaceService
            from app.services.user_service import UserService

            users = UserService(self.db)
            self._memory_service = MemoryService(
                db=self.db, user_service=users,
                space_service=SpaceService(db=self.db, user_service=users),
                embedding_service=EmbeddingService(settings=get_settings()),
                embedding_model_service=EmbeddingModelService(db=self.db, user_service=users),
            )
        return self._memory_service

    def _forget_memory(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """Delete a memory card by memory_id."""
        memory_id = str(arguments.get("memory_id", "")).strip()
        if not memory_id:
            raise DomainValidationError("forget_memory requires a non-empty 'memory_id'.")

        writer = self._memory_writer()
        memory = writer.ensure_access(memory_id=memory_id, team_id=team_id, user_id=user_id)
        title = memory.title or memory_id
        writer.delete(memory_id=memory_id, team_id=team_id, user_id=user_id)
        return {
            "tool_name": "forget_memory",
            "message": f"Memory card '{title}' deleted.",
            "memory_id": memory_id,
        }

    def _recall_memory(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """Semantically search memory cards by query text (keyword fallback)."""
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise DomainValidationError("recall_memory requires a non-empty 'query'.")
        limit = self._parse_limit(arguments.get("limit", 5))
        space_id = self._optional_space_id(team_id=team_id, user_id=user_id, arguments=arguments)

        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_query = f"%{escaped}%"

        stmt = select(MemoryCard).where(
            MemoryCard.team_id == team_id,
            MemoryCard.user_id == user_id,
            or_(
                and_(MemoryCard.scope_level == "global", MemoryCard.space_id.is_(None)),
                MemoryCard.space_id.in_(self._user_space_ids(team_id=team_id, user_id=user_id)),
            ),
            MemoryCard.status == "active",
            or_(MemoryCard.expires_at.is_(None), MemoryCard.expires_at > datetime.now(timezone.utc)),
            or_(
                MemoryCard.title.ilike(like_query, escape="\\"),
                MemoryCard.content.ilike(like_query, escape="\\"),
            ),
        )
        if space_id is not None:
            stmt = stmt.where(or_(
                MemoryCard.space_id == space_id,
                and_(MemoryCard.scope_level == "global", MemoryCard.space_id.is_(None)),
            ))
        stmt = stmt.order_by(
            MemoryCard.updated_at.desc(), MemoryCard.weight.desc(),
        ).limit(limit)

        cards = list(self.db.scalars(stmt).all())
        return {
            "tool_name": "recall_memory",
            "query": query,
            "count": len(cards),
            "memories": [
                {
                    "memory_id": c.memory_id,
                    "title": c.title,
                    "category": c.category,
                    "summary": c.summary or "",
                    "snippet": self._snippet(c.content, query=query),
                    "weight": c.weight,
                    "updated_at": c.updated_at.isoformat(),
                }
                for c in cards
            ],
        }

    def _message_lookup(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """Look up recent chat messages in a conversation."""
        from app.models.chat_history import ChatHistory

        conv_id = str(arguments.get("conversation_id", "")).strip() or None
        limit = self._parse_limit(arguments.get("limit", 5))
        if conv_id:
            conversation = self.db.get(Conversation, conv_id)
            if (conversation is None or conversation.team_id != team_id
                    or conversation.user_id != user_id or conversation.status == "deleted"):
                raise EntityNotFoundError(f"Conversation '{conv_id}' not found.")
            if conversation.space_id is not None:
                self._ensure_space_access(space_id=conversation.space_id, team_id=team_id, user_id=user_id)

        stmt = select(ChatHistory).where(
            ChatHistory.team_id == team_id,
            ChatHistory.user_id == user_id,
            or_(
                ChatHistory.space_id.is_(None),
                ChatHistory.space_id.in_(self._user_space_ids(team_id=team_id, user_id=user_id)),
            ),
        )
        if conv_id:
            stmt = stmt.where(ChatHistory.conversation_id == conv_id)
        stmt = stmt.order_by(ChatHistory.created_at.desc()).limit(limit)

        messages = list(self.db.scalars(stmt).all())
        messages.reverse()  # 最早的在前

        return {
            "tool_name": "message_lookup",
            "count": len(messages),
            "messages": [
                {
                    "message_id": m.message_id,
                    "channel": m.channel,
                    "request": m.request_text,
                    "response": m.response_text[:500] if m.response_text else "",
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
        }

    def _promote_to_conclusion(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        space_id = self._required_space_id(team_id=team_id, user_id=user_id, arguments=arguments)
        title = str(arguments.get("title", "")).strip()
        content = str(arguments.get("content", "")).strip()
        topic = str(arguments.get("topic", "agent")).strip() or "agent"
        status = str(arguments.get("status", "draft")).strip().lower()
        if status not in {"draft", "confirmed", "effective"}:
            raise DomainValidationError("status must be one of: draft, confirmed, effective.")
        if not title or not content:
            raise DomainValidationError("promote_to_conclusion requires non-empty 'title' and 'content'.")

        now = datetime.now(timezone.utc)
        conclusion = Conclusion(
            conclusion_id=str(uuid4()),
            team_id=team_id,
            space_id=space_id,
            user_id=user_id,
            title=title[:128],
            topic=topic[:128],
            content=content[:12000],
            summary=str(arguments.get("summary", "")).strip() or None,
            status=status,
            confidence=0.85,
            effective_from=now if status == "effective" else None,
            effective_to=None,
            source_message_id=None,
            source_favorite_id=None,
            evidence_json=json.dumps(arguments.get("evidence"), ensure_ascii=False)
            if arguments.get("evidence") is not None
            else None,
            tags_json=json.dumps(arguments.get("tags"), ensure_ascii=False)
            if arguments.get("tags") is not None
            else None,
            doc_sync_document_id=None,
            created_at=now,
            updated_at=now,
        )
        self.db.add(conclusion)
        self.db.commit()
        self.db.refresh(conclusion)
        return {
            "tool_name": "promote_to_conclusion",
            "message": "Conclusion created successfully.",
            "conclusion": {
                "conclusion_id": conclusion.conclusion_id,
                "space_id": conclusion.space_id,
                "title": conclusion.title,
                "topic": conclusion.topic,
                "status": conclusion.status,
            },
        }

    def _generate_incident_report(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        title = str(arguments.get("title", "Incident Report")).strip() or "Incident Report"
        summary = str(arguments.get("incident_summary", "")).strip() or "No incident summary provided."
        findings = self._string_list(arguments.get("findings")) or ["Context reviewed by Agent."]
        recommendations = self._string_list(arguments.get("recommendations")) or ["Keep monitoring and update the runbook."]

        lines = [
            f"# {title}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Findings",
            "",
            *[f"- {item}" for item in findings],
            "",
            "## Recommendations",
            "",
            *[f"- {item}" for item in recommendations],
        ]
        report = "\n".join(lines).strip()
        return {
            "tool_name": "generate_incident_report",
            "message": "Incident report generated.",
            "report_markdown": report,
        }

    @staticmethod
    def _user_space_ids(*, team_id: str, user_id: str) -> Select[tuple[str]]:
        return select(ProjectSpace.space_id).where(
            ProjectSpace.team_id == team_id,
            ProjectSpace.owner_user_id == user_id,
            ProjectSpace.status != "deleted",
        )

    def _user_documents(self, *, team_id: str, user_id: str) -> Select[tuple[Document]]:
        owned_conversations = select(Conversation.conversation_id).where(
            Conversation.team_id == team_id,
            Conversation.user_id == user_id,
            Conversation.status != "deleted",
        )
        # Legacy unscoped team-library documents remain shared. Documents
        # attached to a conversation or space must belong to this user.
        return select(Document).where(
            Document.team_id == team_id,
            or_(Document.conversation_id.is_(None), Document.conversation_id.in_(owned_conversations)),
            or_(
                Document.space_id.is_(None),
                Document.space_id.in_(self._user_space_ids(team_id=team_id, user_id=user_id)),
            ),
        )

    def _optional_space_id(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> str | None:
        raw_space_id = str(arguments.get("space_id", "") or "").strip()
        if not raw_space_id:
            return None
        self._ensure_space_access(space_id=raw_space_id, team_id=team_id, user_id=user_id)
        return raw_space_id

    def _required_space_id(
        self,
        *,
        team_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> str:
        space_id = self._optional_space_id(team_id=team_id, user_id=user_id, arguments=arguments)
        if space_id is None:
            raise DomainValidationError("space_id is required for this tool.")
        return space_id

    def _ensure_space_access(self, *, space_id: str, team_id: str, user_id: str) -> None:
        space = self.db.get(ProjectSpace, space_id)
        if space is None or space.status == "deleted":
            raise EntityNotFoundError(f"Space '{space_id}' not found.")
        if space.team_id != team_id or space.owner_user_id != user_id:
            raise EntityNotFoundError(f"Space '{space_id}' not found.")

    def _parse_limit(self, raw_value: object) -> int:
        try:
            limit = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError("limit must be an integer between 1 and 20.") from exc

        if limit < 1 or limit > 20:
            raise DomainValidationError("limit must be an integer between 1 and 20.")
        return limit

    def _snippet(self, content: str, *, query: str) -> str:
        normalized = " ".join(str(content or "").split())
        if not normalized:
            return ""
        index = normalized.lower().find(query.lower())
        if index < 0:
            return normalized[:240]
        start = max(0, index - 80)
        end = min(len(normalized), index + len(query) + 160)
        return normalized[start:end]

    def _string_list(self, raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        items: list[str] = []
        for item in raw:
            value = str(item).strip()
            if value:
                items.append(value)
        return items
