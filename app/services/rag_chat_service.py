from app.schemas.chat import ChatAskRequest, ChatAskResponse, ChatContentPart, ChatSource
from app.schemas.retrieval import RetrievalHit
from app.models.document import Document
from app.services.chat_history_service import ChatHistoryService
from app.services.document_service import DocumentService
from app.services.llm_model_service import LLMModelService
from app.services.llm_service import AssistantContentPart, LLMAnswer, LLMService, VisionAttachment
from app.core.config import get_settings
from app.services.memory_service import MemoryService
from app.services.memory_markdown_store import MemoryMarkdownStore
from app.services.persona_prompt import PersonaPromptBuilder
from app.services.retrieval_enhancer import EnhancedRetrievalService
from app.services.retrieval_service import RetrievalService
from app.services.user_service import UserService


class RagChatService:
    def __init__(
        self,
        user_service: UserService,
        chat_history_service: ChatHistoryService,
        document_service: DocumentService,
        retrieval_service: RetrievalService,
        memory_service: MemoryService,
        llm_service: LLMService,
        llm_model_service: LLMModelService,
        enhanced_retrieval: EnhancedRetrievalService | None = None,
        memory_store: MemoryMarkdownStore | None = None,
    ) -> None:
        self.user_service = user_service
        self.chat_history_service = chat_history_service
        self.document_service = document_service
        self.retrieval_service = retrieval_service
        self.memory_service = memory_service
        self.llm_service = llm_service
        self.llm_model_service = llm_model_service
        self.enhanced_retrieval = enhanced_retrieval
        self.persona_prompt_builder = PersonaPromptBuilder(memory_store=memory_store)

    def ask(self, payload: ChatAskRequest, *, before_message_id: str | None = None) -> ChatAskResponse:
        self.user_service.ensure_user_in_team(
            user_id=payload.user_id,
            team_id=payload.team_id,
        )
        if isinstance(payload.use_document_scope, bool):
            use_document_scope = payload.use_document_scope
        else:
            use_document_scope = bool(
                payload.document_id
                or payload.selected_document_ids
                or payload.include_library
                or payload.include_conclusions
            )
        effective_space_id = self.document_service.resolve_space_id(
            team_id=payload.team_id,
            conversation_id=payload.conversation_id,
            space_id=payload.space_id,
            user_id=payload.user_id,
        )
        selected_document_ids = self._resolve_selected_document_ids(payload) if use_document_scope else None
        conversation_messages = self._build_conversation_messages(
            team_id=payload.team_id,
            user_id=payload.user_id,
            conversation_id=payload.conversation_id,
            before_message_id=before_message_id,
        )
        scope_documents = (
            self.document_service.get_documents_in_scope(
                team_id=payload.team_id,
                conversation_id=payload.conversation_id,
                space_id=effective_space_id,
                document_ids=selected_document_ids,
                include_library=payload.include_library,
                include_conclusions=payload.include_conclusions,
                ready_only=True,
            )
            if use_document_scope
            else []
        )
        scope_document_ids = [item.document_id for item in scope_documents]
        image_attachments = self._build_image_attachments(scope_documents)
        fallback_text_context = self._build_attachment_text_fallback(scope_documents)
        memory_messages = self._build_memory_messages(
            team_id=payload.team_id,
            user_id=payload.user_id,
            space_id=effective_space_id,
            question=payload.question,
            include_memory=payload.include_memory,
            embedding_model=payload.embedding_model,
        )
        llm_context_messages = [*conversation_messages, *memory_messages]
        persona = self.persona_prompt_builder.build(
            system_prompt=None,
            channel="web",
            team_id=payload.team_id,
            user_id=payload.user_id,
            space_id=effective_space_id,
            include_memory=payload.include_memory,
        )

        requested_model = payload.model.strip() if payload.model else None
        force_mock = False
        if requested_model and requested_model.lower() == "default":
            requested_model = None
        if requested_model and requested_model.lower() == "none":
            requested_model = None
            force_mock = True

        runtime_model = self.llm_model_service.resolve_runtime_config(
            team_id=payload.team_id,
            user_id=payload.user_id,
            model_name=requested_model,
        )
        if runtime_model is not None:
            selected_model = runtime_model.model_name
            runtime_selected_model = runtime_model.model_name
        elif force_mock:
            selected_model = "none"
            runtime_selected_model = None
        elif requested_model:
            selected_model = requested_model
            runtime_selected_model = requested_model
        else:
            selected_model = "default"
            runtime_selected_model = None

        should_use_rag = use_document_scope and bool(scope_document_ids) and self.retrieval_service.has_indexed_chunks(
            team_id=payload.team_id,
            document_ids=scope_document_ids,
        )

        if not should_use_rag:
            llm_answer = self.llm_service.answer_chat(
                message=payload.question,
                model=runtime_selected_model,
                base_url=runtime_model.base_url if runtime_model is not None else None,
                api_key=runtime_model.api_key if runtime_model is not None else None,
                force_mock=force_mock,
                image_attachments=image_attachments,
                fallback_text_context=fallback_text_context,
                conversation_messages=llm_context_messages,
                system_prompt=persona.system_prompt,
            )
            return ChatAskResponse.from_result(
                user_id=payload.user_id,
                team_id=payload.team_id,
                conversation_id=payload.conversation_id,
                space_id=effective_space_id,
                question=payload.question,
                answer=llm_answer.answer,
                content_parts=self._build_content_parts(llm_answer),
                hits=[],
                mode="chat",
                sources=[],
                model=selected_model,
            )

        if self.enhanced_retrieval:
            settings = get_settings()
            fast_model = settings.llm_fast_model or settings.llm_model or runtime_selected_model or ""
            fast_base = settings.llm_fast_base_url or settings.llm_base_url or ""
            fast_key = settings.llm_fast_api_key or settings.llm_api_key or ""
            if not fast_base or not fast_key:
                fast_model = fast_base = fast_key = ""  # let LLMService use defaults
            enhanced = self.enhanced_retrieval.search_chunks_enhanced(
                query=payload.question,
                team_id=payload.team_id,
                user_id=payload.user_id,
                fast_model_name=fast_model,
                fast_base_url=fast_base,
                fast_api_key=fast_key,
                top_k=payload.top_k,
                document_ids=scope_document_ids or None,
                embedding_model=payload.embedding_model,
            )
            raw_hits = enhanced.hits
        else:
            raw_hits = self.retrieval_service.search_chunks(
                team_id=payload.team_id,
                query=payload.question,
                top_k=payload.top_k,
                document_ids=scope_document_ids or None,
                user_id=payload.user_id,
                embedding_model=payload.embedding_model,
            )

        llm_answer = self.llm_service.answer_question(
            question=payload.question,
            hits=raw_hits,
            model=runtime_selected_model,
            base_url=runtime_model.base_url if runtime_model is not None else None,
            api_key=runtime_model.api_key if runtime_model is not None else None,
            force_mock=force_mock,
            image_attachments=image_attachments,
            conversation_messages=llm_context_messages,
            system_prompt=persona.system_prompt,
        )

        hits = [RetrievalHit.model_validate(item) for item in raw_hits]
        sources = self._build_sources(raw_hits)
        return ChatAskResponse.from_result(
            user_id=payload.user_id,
            team_id=payload.team_id,
            conversation_id=payload.conversation_id,
            space_id=effective_space_id,
            question=payload.question,
            answer=llm_answer.answer,
            content_parts=self._build_content_parts(llm_answer),
            hits=hits,
            mode="rag",
            sources=sources,
            model=selected_model,
        )

    def _build_sources(self, raw_hits: list[dict[str, object]]) -> list[ChatSource]:
        seen: set[str] = set()
        sources: list[ChatSource] = []
        for hit in raw_hits:
            chunk_id = str(hit.get("chunk_id", "")).strip()
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            sources.append(
                ChatSource(
                    document_id=str(hit.get("document_id", "")),
                    source_name=(str(hit.get("source_name", "")).strip() or None),
                    chunk_id=chunk_id,
                    chunk_index=int(hit.get("chunk_index", 0)),
                    page_no=int(hit.get("page_no")) if hit.get("page_no") is not None else None,
                    locator_label=(str(hit.get("locator_label", "")).strip() or None),
                    snippet=self._build_snippet(hit.get("content")),
                    score=float(hit.get("score", 0.0)),
                )
            )
        return sources

    def _build_image_attachments(self, documents: list[Document]) -> list[VisionAttachment]:
        raw_items = self.document_service.build_chat_image_attachments(documents=documents)
        attachments: list[VisionAttachment] = []
        for item in raw_items:
            data_url = str(item.get("data_url", "")).strip()
            if not data_url:
                continue
            attachments.append(
                VisionAttachment(
                    document_id=str(item.get("document_id", "")).strip(),
                    source_name=str(item.get("source_name", "")).strip(),
                    mime_type=str(item.get("mime_type", "")).strip(),
                    data_url=data_url,
                )
            )
        return attachments

    def _build_attachment_text_fallback(self, documents: list[Document]) -> str | None:
        parts: list[str] = []
        for item in documents:
            content_type = str(getattr(item, "content_type", "")).strip().lower()
            if content_type not in {"png", "jpg", "jpeg", "webp"}:
                continue
            source_name = str(getattr(item, "source_name", "")).strip() or "image"
            content = str(getattr(item, "content", "")).strip()
            if not content:
                continue
            parts.append(f"[{source_name}]\n{content}")
        if not parts:
            return None
        return "\n\n".join(parts)

    def _build_conversation_messages(
        self,
        *,
        team_id: str,
        user_id: str,
        conversation_id: str | None,
        before_message_id: str | None,
    ) -> list[dict[str, object]]:
        history_turns = max(0, int(self.llm_service.settings.llm_history_turns))
        if conversation_id is None or history_turns < 1:
            return []

        history_items = self.chat_history_service.list_messages_for_context(
            team_id=team_id,
            user_id=user_id,
            conversation_id=conversation_id,
            limit=history_turns,
            before_message_id=before_message_id,
        )

        messages: list[dict[str, object]] = []
        for item in history_items:
            channel = str(getattr(item, "channel", "")).strip().lower()
            if channel not in {"ask", "echo"}:
                continue

            request_text = str(getattr(item, "request_text", "")).strip()
            response_text = str(getattr(item, "response_text", "")).strip()
            if request_text:
                messages.append({"role": "user", "content": request_text})
            if response_text:
                messages.append({"role": "assistant", "content": response_text})
        return messages

    def _build_memory_messages(
        self,
        *,
        team_id: str,
        user_id: str,
        space_id: str | None,
        question: str,
        include_memory: bool,
        embedding_model: str | None,
    ) -> list[dict[str, object]]:
        if not include_memory or space_id is None:
            return []

        memory_hits = self.memory_service.search_cards_for_chat(
            team_id=team_id,
            user_id=user_id,
            space_id=space_id,
            query=question,
            top_k=3,
            embedding_model=embedding_model,
        )
        if not memory_hits:
            return []

        lines = ["Long-term memory for this workspace (use as constraints/preferences):"]
        for index, item in enumerate(memory_hits, start=1):
            title = str(item.get("title", "")).strip() or f"Memory {index}"
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"- {title}: {content}")
        if len(lines) == 1:
            return []

        return [{"role": "assistant", "content": "\n".join(lines)}]

    def _build_content_parts(self, answer: LLMAnswer) -> list[ChatContentPart]:
        items: list[ChatContentPart] = []
        for part in answer.content_parts:
            items.append(self._build_content_part(part))
        return items

    def _build_content_part(self, part: AssistantContentPart) -> ChatContentPart:
        return ChatContentPart(
            type="image" if part.type == "image" else "text",
            text=part.text,
            url=part.url,
            original_url=part.original_url,
            mime_type=part.mime_type,
            alt=part.alt,
        )

    def _resolve_selected_document_ids(self, payload: ChatAskRequest) -> list[str] | None:
        values: list[str] = []
        if payload.document_id:
            values.append(payload.document_id)
        if payload.selected_document_ids:
            values.extend(payload.selected_document_ids)

        deduped: list[str] = []
        for item in values:
            normalized = str(item).strip()
            if normalized and normalized not in deduped:
                deduped.append(normalized)
        return deduped or None

    def _build_snippet(self, raw_content: object) -> str | None:
        if not isinstance(raw_content, str):
            return None
        text = " ".join(raw_content.split())
        if not text:
            return None
        return text[:220]
