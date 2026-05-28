from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import DomainValidationError
from app.services.model_base_url import normalize_openai_compatible_base_url


@dataclass(frozen=True)
class VisionAttachment:
    document_id: str
    source_name: str
    mime_type: str
    data_url: str


@dataclass(frozen=True)
class AssistantContentPart:
    type: str
    text: str | None = None
    url: str | None = None
    original_url: str | None = None
    mime_type: str | None = None
    alt: str | None = None


@dataclass(frozen=True)
class LLMAnswer:
    answer: str
    content_parts: tuple[AssistantContentPart, ...] = ()


@dataclass(frozen=True)
class LLMCompletionResult:
    assistant_text: str
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    finish_reason: str | None = None
    raw_message: dict[str, object] | None = None
    usage: dict[str, object] | None = None
    model: str | None = None


@dataclass(frozen=True)
class LLMStreamChunk:
    delta_text: str | None = None
    tool_call_deltas: list[dict[str, object]] = field(default_factory=list)
    finish_reason: str | None = None


class LLMService:
    """LLM wrapper with a default local mock mode for beginner-friendly setup."""
    _CONTINUE_PROMPT = "Continue exactly from where you stopped. Do not repeat prior text. Finish the current answer."
    _MAX_COMPLETION_SEGMENTS = 4
    _IMAGE_GENERATION_TIMEOUT_SECONDS = 90.0
    _MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)\)")
    _HTML_TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
    _IMAGE_MIME_BY_SUFFIX = {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def answer_question(
        self,
        question: str,
        hits: list[dict[str, object]],
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        force_mock: bool = False,
        image_attachments: list[VisionAttachment] | None = None,
        conversation_messages: list[dict[str, object]] | None = None,
    ) -> LLMAnswer:
        if force_mock:
            return self._mock_answer(question=question, hits=hits)

        runtime = self._resolve_runtime(base_url=base_url, api_key=api_key)
        if runtime is None:
            return self._mock_answer(question=question, hits=hits)

        return self._openai_compatible_answer(
            question=question,
            hits=hits,
            model=model,
            base_url=runtime[0],
            api_key=runtime[1],
            image_attachments=image_attachments,
            conversation_messages=conversation_messages,
        )

    def answer_chat(
        self,
        message: str,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        force_mock: bool = False,
        image_attachments: list[VisionAttachment] | None = None,
        fallback_text_context: str | None = None,
        conversation_messages: list[dict[str, object]] | None = None,
    ) -> LLMAnswer:
        """General chat answer without retrieval context."""
        if force_mock:
            return self._mock_chat_answer(message=message)

        runtime = self._resolve_runtime(base_url=base_url, api_key=api_key)
        if runtime is None:
            return self._mock_chat_answer(message=message)

        return self._openai_compatible_chat_answer(
            message=message,
            model=model,
            base_url=runtime[0],
            api_key=runtime[1],
            image_attachments=image_attachments,
            fallback_text_context=fallback_text_context,
            conversation_messages=conversation_messages,
        )

    # ------------------------------------------------------------------
    # Low-level generic interfaces (Phase 1: tool-calling + streaming)
    # ------------------------------------------------------------------

    def complete_chat(
        self,
        *,
        messages: list[dict[str, object]],
        model: str | None = None,
        base_url: str = "",
        api_key: str = "",
        tools: list[dict[str, object]] | None = None,
        tool_choice: str = "auto",
        force_mock: bool = False,
        timeout_seconds: float | None = None,
    ) -> LLMCompletionResult:
        """Non-streaming chat completion with optional tool calling."""
        if force_mock:
            return self._mock_complete(messages)

        runtime = self._resolve_runtime(base_url=base_url, api_key=api_key)
        if runtime is None:
            return self._mock_complete(messages)

        payload = self._build_payload(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        selected_model = str(model or self.settings.llm_model).strip()
        effective_timeout = timeout_seconds if timeout_seconds is not None else self.settings.llm_timeout_seconds

        try:
            response = self._post_chat_completion(
                payload=payload,
                base_url=runtime[0],
                api_key=runtime[1],
                timeout_seconds=effective_timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._extract_http_error_detail(exc.response)
            if self._detect_tools_unsupported(detail):
                raise DomainValidationError(f"LLM_TOOLS_UNSUPPORTED: {detail}") from exc
            raise DomainValidationError(f"LLM request failed: {detail}") from exc
        except httpx.HTTPError as exc:
            raise DomainValidationError(f"LLM request failed: {exc}") from exc

        body = response.json()
        return self._parse_completion(body, model=selected_model)

    def stream_chat(
        self,
        *,
        messages: list[dict[str, object]],
        model: str | None = None,
        base_url: str = "",
        api_key: str = "",
        tools: list[dict[str, object]] | None = None,
        tool_choice: str = "auto",
        force_mock: bool = False,
    ) -> Iterator[LLMStreamChunk]:
        """Streaming chat completion yielding LLMStreamChunk events."""
        if force_mock:
            yield from self._mock_stream(messages)
            return

        runtime = self._resolve_runtime(base_url=base_url, api_key=api_key)
        if runtime is None:
            yield from self._mock_stream(messages)
            return

        payload = self._build_payload(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            stream=True,
        )

        try:
            with self._post_chat_completion_stream(
                payload=payload,
                base_url=runtime[0],
                api_key=runtime[1],
                timeout_seconds=self.settings.llm_timeout_seconds,
            ) as response:
                response.raise_for_status()
                yield from self._consume_sse_stream(response)
        except httpx.HTTPStatusError as exc:
            detail = self._extract_http_error_detail(exc.response)
            if self._detect_stream_unsupported(detail):
                raise DomainValidationError(f"LLM_STREAM_UNSUPPORTED: {detail}") from exc
            raise DomainValidationError(f"LLM request failed: {detail}") from exc
        except httpx.HTTPError as exc:
            raise DomainValidationError(f"LLM request failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Tool parsing
    # ------------------------------------------------------------------

    def _parse_tool_calls(self, message: dict[str, object]) -> list[dict[str, object]]:
        """Parse tool_calls from an assistant message, normalizing arguments from JSON strings."""
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            return []

        parsed: list[dict[str, object]] = []
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = __import__("json").loads(raw_args)
                except (TypeError, ValueError):
                    args = raw_args
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            parsed.append({
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args,
                },
            })
        return parsed

    def _parse_completion(
        self,
        body: dict[str, object],
        *,
        model: str,
    ) -> LLMCompletionResult:
        try:
            choice = body["choices"][0]  # type: ignore[index]
            message = choice["message"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise DomainValidationError("LLM response format is invalid.") from exc

        assistant_text = ""
        if isinstance(message, dict):
            content = message.get("content", "")
            assistant_text = str(content) if content is not None else ""

        tool_calls = self._parse_tool_calls(message) if isinstance(message, dict) else []
        finish_reason = str(choice.get("finish_reason", "")).strip() or None if isinstance(choice, dict) else None
        usage: dict[str, object] | None = None
        raw_usage = body.get("usage")
        if isinstance(raw_usage, dict):
            usage = raw_usage

        return LLMCompletionResult(
            assistant_text=assistant_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw_message=message if isinstance(message, dict) else None,
            usage=usage,
            model=model,
        )

    def _consume_sse_stream(
        self,
        response: httpx.Response,
    ) -> Iterator[LLMStreamChunk]:
        """Consume SSE streaming response, yielding LLMStreamChunk events.

        Compatible with both httpx.stream() (returns bytes lines) and
        older buffered responses (where iter_lines may return str).
        """
        tool_index: dict[str, int] = {}
        accumulated_tool_args: dict[str, str] = {}
        accumulated_tool_names: dict[str, str] = {}
        finished = False

        for raw_line in response.iter_lines():
            if finished:
                break
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw_line).strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                finished = True
                continue
            if not line.startswith("data: "):
                continue

            data_str = line.removeprefix("data: ")
            try:
                data = __import__("json").loads(data_str)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue

            try:
                choice = data["choices"][0]  # type: ignore[index]
            except (KeyError, IndexError, TypeError):
                continue

            delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
            finish_reason = str(choice.get("finish_reason", "")).strip() or None if isinstance(choice, dict) else None

            delta_text = None
            if isinstance(delta, dict):
                content = delta.get("content", "")
                if content:
                    delta_text = str(content)

            tool_deltas: list[dict[str, object]] = []
            if isinstance(delta, dict) and "tool_calls" in delta:
                raw_tool_deltas = delta["tool_calls"]
                if isinstance(raw_tool_deltas, list):
                    for tc in raw_tool_deltas:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        if idx is None:
                            continue
                        key = str(idx)
                        if key not in tool_index:
                            tool_index[key] = len(tool_index)
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                        fn_name = fn.get("name", "")
                        if fn_name:
                            accumulated_tool_names[key] = str(fn_name)
                        fn_args = fn.get("arguments", "")
                        if fn_args:
                            accumulated_tool_args[key] = accumulated_tool_args.get(key, "") + str(fn_args)
                        tool_deltas.append({
                            "index": int(idx),
                            "function": {"name": fn_name, "arguments": fn_args},
                        })

            if delta_text or tool_deltas or finish_reason:
                yield LLMStreamChunk(
                    delta_text=delta_text,
                    tool_call_deltas=tool_deltas,
                    finish_reason=finish_reason,
                )

    def finalize_tool_calls_from_stream(
        self,
        accumulated_names: dict[str, str],
        accumulated_args: dict[str, str],
        ordered_indices: dict[str, int],
    ) -> list[dict[str, object]]:
        """After streaming ends, build final tool_calls from accumulated deltas."""
        result: list[dict[str, object]] = []
        seen: list[tuple[int, str, dict[str, object]]] = []
        for key, idx in ordered_indices.items():
            name = accumulated_names.get(key, "")
            args_str = accumulated_args.get(key, "")
            try:
                args = __import__("json").loads(args_str)
            except (TypeError, ValueError):
                args = {}
            seen.append((idx, name, args))
        seen.sort(key=lambda x: x[0])

        # Build ids matching index-based convention
        for _, name, args in seen:
            import uuid
            result.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            })
        return result

    def _mock_complete(self, messages: list[dict[str, object]]) -> LLMCompletionResult:
        last_user = ""
        has_tool_result = False
        for msg in reversed(messages):
            role = str(msg.get("role", "")).strip().lower()
            if role == "tool":
                has_tool_result = True
            if role == "user":
                last_user = str(msg.get("content", ""))
                break

        # When a tool result was already received, respond with text instead of more tools
        if has_tool_result:
            return LLMCompletionResult(
                assistant_text=f"[Mock Agent] Tool completed for: {last_user[:200]}",
                model="mock",
            )

        import uuid

        # Lightweight keyword-based mock tool calling for dev/test realism
        tool_calls: list[dict[str, object]] = []
        task = last_user

        if any(kw in task for kw in ["create incident", "incident", "创建 incident", "创建 incident",
                                       "创建事件", "新建事件", "创建故障", "告警", "故障", "事故"]):
            if "不要创建" not in task and "do not create" not in task.lower():
                severity = "P2"
                import re
                p_match = re.search(r"\bP([0-9])\b", task, re.IGNORECASE)
                if p_match:
                    severity = f"P{p_match.group(1)}" .upper()
                elif any(t in task.lower() for t in ["critical", "紧急", "严重", "宕机"]):
                    severity = "P1"
                elif any(t in task.lower() for t in ["minor", "低优先级", "轻微"]):
                    severity = "P3"
                title_match = re.search(r"(?:：|:)\s*(.+)", task)
                title = title_match.group(1)[:255] if title_match else task[:255]
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": "create_incident", "arguments": {"title": title, "severity": severity}},
                })

        if any(kw in task for kw in ["search knowledge", "知识库", "runbook", "手册", "查资料"]):
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "search_knowledge", "arguments": {"query": task, "limit": 5}},
            })

        if any(kw in task for kw in ["recent document", "list documents", "最近文档", "查看文档", "列出文档", "查文档"]):
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "list_recent_documents", "arguments": {"limit": 5}},
            })

        if any(kw in task for kw in ["create memory", "memory card", "记忆卡", "长期记忆", "沉淀记忆"]):
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "create_memory_card", "arguments": {"title": task[:128], "content": task}},
            })

        if any(kw in task for kw in ["promote to conclusion", "create conclusion", "沉淀结论", "保存结论"]):
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "promote_to_conclusion", "arguments": {"title": task[:128], "content": task}},
            })

        if any(kw in task for kw in ["incident report", "postmortem", "事件报告", "处理报告"]):
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": "generate_incident_report", "arguments": {"incident_summary": task}},
            })

        assistant_text = ""
        if not tool_calls:
            assistant_text = f"[Mock Agent] {last_user}" if last_user else "[Mock Agent] No user input."
        return LLMCompletionResult(
            assistant_text=assistant_text,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            model="mock",
        )

    def _mock_stream(self, messages: list[dict[str, object]]) -> Iterator[LLMStreamChunk]:
        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user = str(msg.get("content", ""))
                break
        answer = f"[Mock Agent] {last_user}" if last_user else "[Mock Agent] No user input."
        yield LLMStreamChunk(delta_text=answer, finish_reason="stop")

    def _detect_tools_unsupported(self, error_detail: str) -> bool:
        normalized = error_detail.lower()
        keywords = ["tools", "tool_choice", "function calling", "does not support"]
        return any(kw in normalized for kw in keywords)

    def _detect_stream_unsupported(self, error_detail: str) -> bool:
        normalized = error_detail.lower()
        keywords = ["stream", "streaming"]
        return any(kw in normalized for kw in keywords)

    # ------------------------------------------------------------------
    # Existing methods (unchanged)
    # ------------------------------------------------------------------

    def _mock_answer(self, question: str, hits: list[dict[str, object]]) -> LLMAnswer:
        if not hits:
            answer = "No relevant knowledge chunks were found, so I cannot answer this yet."
            return self._build_text_answer(answer)

        candidates = self._extract_candidate_sentences(hits)
        if not candidates:
            answer = "Chunks were retrieved, but their content is empty, so no answer can be generated."
            return self._build_text_answer(answer)

        answer = self._pick_best_sentence(question=question, candidates=candidates)
        return self._build_text_answer(f"[Mock Answer] {answer}")

    def _mock_chat_answer(self, message: str) -> LLMAnswer:
        normalized = message.strip()
        if not normalized:
            return self._build_text_answer("[Mock Chat] Please tell me what you want to discuss.")
        return self._build_text_answer(f"[Mock Chat] {normalized}")

    def _openai_compatible_answer(
        self,
        question: str,
        hits: list[dict[str, object]],
        model: str | None = None,
        base_url: str = "",
        api_key: str = "",
        image_attachments: list[VisionAttachment] | None = None,
        conversation_messages: list[dict[str, object]] | None = None,
    ) -> LLMAnswer:
        context = self._build_context(hits)

        system_prompt = (
            "You are CaiBao, an enterprise assistant. Use prior conversation only to resolve references and user intent. "
            "Answer strictly based on the provided context for factual claims. If context is insufficient, say so explicitly."
        )
        if image_attachments:
            system_prompt += " Attached images are primary evidence when relevant. Use context as supporting material."
        user_prompt = f"Question:\n{question}\n\nContext:\n{context}"

        try:
            return self._request_chat_answer(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                base_url=base_url,
                api_key=api_key,
                image_attachments=image_attachments,
                conversation_messages=conversation_messages,
            )
        except DomainValidationError as exc:
            if image_attachments and self._should_retry_without_images(str(exc)):
                return self._request_chat_answer(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    base_url=base_url,
                    api_key=api_key,
                    image_attachments=None,
                    conversation_messages=conversation_messages,
                )
            raise

    def _openai_compatible_chat_answer(
        self,
        message: str,
        model: str | None = None,
        base_url: str = "",
        api_key: str = "",
        image_attachments: list[VisionAttachment] | None = None,
        fallback_text_context: str | None = None,
        conversation_messages: list[dict[str, object]] | None = None,
    ) -> LLMAnswer:
        system_prompt = "You are CaiBao, a helpful enterprise assistant."
        try:
            return self._request_chat_answer(
                model=model,
                system_prompt=system_prompt,
                user_prompt=message,
                base_url=base_url,
                api_key=api_key,
                image_attachments=image_attachments,
                conversation_messages=conversation_messages,
            )
        except DomainValidationError as exc:
            if image_attachments and self._should_retry_without_images(str(exc)):
                fallback_prompt = self._build_fallback_chat_prompt(
                    message=message,
                    fallback_text_context=fallback_text_context,
                )
                return self._request_chat_answer(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=fallback_prompt,
                    base_url=base_url,
                    api_key=api_key,
                    image_attachments=None,
                    conversation_messages=conversation_messages,
                )
            raise

    def _build_payload(
        self,
        model: str | None,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str = "auto",
        stream: bool = False,
    ) -> dict[str, object]:
        selected_model = model.strip() if model else self.settings.llm_model
        payload: dict[str, object] = {
            "model": selected_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
            payload["parallel_tool_calls"] = False
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _request_chat_answer(
        self,
        *,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        base_url: str,
        api_key: str,
        image_attachments: list[VisionAttachment] | None,
        conversation_messages: list[dict[str, object]] | None,
    ) -> LLMAnswer:
        normalized_history = self._normalize_conversation_messages(conversation_messages)
        history_mode = self._resolve_history_mode()
        if not normalized_history or history_mode == "native":
            return self._request_chat_answer_once(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                base_url=base_url,
                api_key=api_key,
                image_attachments=image_attachments,
                conversation_messages=normalized_history,
                history_mode="native",
            )
        if history_mode == "compat":
            return self._request_chat_answer_once(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                base_url=base_url,
                api_key=api_key,
                image_attachments=image_attachments,
                conversation_messages=normalized_history,
                history_mode="compat",
            )

        try:
            return self._request_chat_answer_once(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                base_url=base_url,
                api_key=api_key,
                image_attachments=image_attachments,
                conversation_messages=normalized_history,
                history_mode="native",
            )
        except DomainValidationError as exc:
            if not self._should_retry_with_history_compat(str(exc)):
                raise
            return self._request_chat_answer_once(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                base_url=base_url,
                api_key=api_key,
                image_attachments=image_attachments,
                conversation_messages=normalized_history,
                history_mode="compat",
            )

    def _request_chat_answer_once(
        self,
        *,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        base_url: str,
        api_key: str,
        image_attachments: list[VisionAttachment] | None,
        conversation_messages: list[dict[str, object]],
        history_mode: str,
    ) -> LLMAnswer:
        messages = self._build_initial_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_attachments=image_attachments,
            conversation_messages=conversation_messages,
            history_mode=history_mode,
        )
        request_timeout = self._resolve_request_timeout(
            user_prompt=self._resolve_timeout_prompt(
                user_prompt=user_prompt,
                conversation_messages=conversation_messages,
                history_mode=history_mode,
            )
        )
        return self._run_chat_completion_loop(
            model=model,
            messages=messages,
            base_url=base_url,
            api_key=api_key,
            request_timeout=request_timeout,
        )

    def _run_chat_completion_loop(
        self,
        *,
        model: str | None,
        messages: list[dict[str, object]],
        base_url: str,
        api_key: str,
        request_timeout: float,
    ) -> LLMAnswer:
        answer_parts: list[str] = []
        content_parts: list[AssistantContentPart] = []

        for _ in range(self._MAX_COMPLETION_SEGMENTS):
            payload = self._build_payload(model=model, messages=messages)
            try:
                response = self._post_chat_completion(
                    payload=payload,
                    base_url=base_url,
                    api_key=api_key,
                    timeout_seconds=request_timeout,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = self._extract_http_error_detail(exc.response)
                raise DomainValidationError(f"LLM request failed: {detail}") from exc
            except httpx.HTTPError as exc:
                raise DomainValidationError(f"LLM request failed: {exc}") from exc

            body = response.json()
            answer_part, finish_reason = self._parse_llm_answer(body)
            if answer_part.answer:
                answer_parts.append(answer_part.answer)
            content_parts.extend(answer_part.content_parts)

            if finish_reason != "length" or not answer_part.answer.strip():
                break

            messages.append({"role": "assistant", "content": answer_part.answer})
            messages.append({"role": "user", "content": self._CONTINUE_PROMPT})

        answer = "".join(answer_parts).strip()
        if not answer and any(part.type == "image" and part.url for part in content_parts):
            answer = "Image output"
        if not answer and not content_parts:
            raise DomainValidationError("LLM returned an empty answer.")
        return LLMAnswer(
            answer=answer,
            content_parts=tuple(self._coalesce_content_parts(content_parts)),
        )

    def _build_initial_messages(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_attachments: list[VisionAttachment] | None,
        conversation_messages: list[dict[str, object]],
        history_mode: str,
    ) -> list[dict[str, object]]:
        if history_mode == "compat":
            compat_user_prompt = self._build_history_compat_user_prompt(
                user_prompt=user_prompt,
                conversation_messages=conversation_messages,
            )
            return [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": self._build_user_content(
                        user_prompt=compat_user_prompt,
                        image_attachments=image_attachments,
                    ),
                },
            ]

        return [
            {"role": "system", "content": system_prompt},
            *conversation_messages,
            {
                "role": "user",
                "content": self._build_user_content(
                    user_prompt=user_prompt,
                    image_attachments=image_attachments,
                ),
            },
        ]

    def _resolve_timeout_prompt(
        self,
        *,
        user_prompt: str,
        conversation_messages: list[dict[str, object]],
        history_mode: str,
    ) -> str:
        if history_mode != "compat":
            return user_prompt
        return self._build_history_compat_user_prompt(
            user_prompt=user_prompt,
            conversation_messages=conversation_messages,
        )

    def _build_user_content(
        self,
        *,
        user_prompt: str,
        image_attachments: list[VisionAttachment] | None,
    ) -> str | list[dict[str, object]]:
        if not image_attachments:
            return user_prompt

        content: list[dict[str, object]] = [{"type": "text", "text": user_prompt}]
        for item in image_attachments:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": item.data_url,
                        "detail": "auto",
                    },
                }
            )
        return content

    def _build_fallback_chat_prompt(self, *, message: str, fallback_text_context: str | None) -> str:
        normalized_context = (fallback_text_context or "").strip()
        if not normalized_context:
            return message
        return f"{message}\n\nAttachment text fallback:\n{normalized_context}"

    def _normalize_conversation_messages(
        self,
        conversation_messages: list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        normalized_messages: list[dict[str, object]] = []
        for item in conversation_messages or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            content = item.get("content")
            if role not in {"user", "assistant"}:
                continue
            if not isinstance(content, str):
                continue
            normalized_content = content.strip()
            if not normalized_content:
                continue
            normalized_messages.append({"role": role, "content": normalized_content})
        return normalized_messages

    def _build_history_compat_user_prompt(
        self,
        *,
        user_prompt: str,
        conversation_messages: list[dict[str, object]],
    ) -> str:
        if not conversation_messages:
            return user_prompt

        lines = ["Conversation history:"]
        for item in conversation_messages:
            role = "User" if item["role"] == "user" else "Assistant"
            lines.append(f"{role}: {item['content']}")
        lines.append("")
        lines.append("Current user request:")
        lines.append(user_prompt)
        return "\n".join(lines)

    def _resolve_history_mode(self) -> str:
        normalized = str(self.settings.llm_history_mode or "").strip().lower()
        if normalized in {"native", "compat", "auto"}:
            return normalized
        return "auto"

    def _should_retry_with_history_compat(self, error_message: str) -> bool:
        normalized = error_message.lower()
        no_retry_keywords = [
            "api key",
            "authentication",
            "unauthorized",
            "forbidden",
            "permission denied",
            "insufficient quota",
            "quota exceeded",
            "billing",
            "rate limit",
        ]
        if any(keyword in normalized for keyword in no_retry_keywords):
            return False
        return True

    def _post_chat_completion(
        self,
        payload: dict[str, object],
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
    ) -> httpx.Response:
        normalized_base_url = normalize_openai_compatible_base_url(base_url)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        return httpx.post(
            f"{normalized_base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout_seconds,
        )

    def _post_chat_completion_stream(
        self,
        payload: dict[str, object],
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
    ):
        """Streaming HTTP request that returns a context-managed response for SSE consumption."""
        normalized_base_url = normalize_openai_compatible_base_url(base_url)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload["stream"] = True
        if "stream_options" not in payload:
            payload["stream_options"] = {"include_usage": True}
        return httpx.stream(
            "POST",
            f"{normalized_base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout_seconds,
        )

    def _resolve_runtime(self, *, base_url: str | None, api_key: str | None) -> tuple[str, str] | None:
        runtime_base_url = (base_url or "").strip()
        runtime_api_key = (api_key or "").strip()
        if runtime_base_url or runtime_api_key:
            if not runtime_base_url or not runtime_api_key:
                raise DomainValidationError("Both base_url and api_key are required for custom model.")
            return runtime_base_url, runtime_api_key

        provider = self.settings.llm_provider.lower().strip()
        settings_base_url = self.settings.llm_base_url.strip()
        settings_key = (self.settings.llm_api_key or "").strip()

        if provider == "mock":
            # Compat: if user filled .env base_url + api_key but forgot to switch provider,
            # treat default runtime as real provider instead of forcing mock.
            if settings_base_url and settings_key:
                return settings_base_url, settings_key
            return None

        if not settings_key:
            raise DomainValidationError("LLM_API_KEY is required when llm_provider is not 'mock'.")
        return settings_base_url, settings_key

    def _extract_http_error_detail(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            detail = body.get("error") or body.get("detail") or body.get("message")
            if isinstance(detail, dict):
                message = detail.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            if isinstance(detail, str) and detail.strip():
                return detail.strip()

        text = response.text.strip()
        if text:
            if self._looks_like_html_document(text):
                title = self._extract_html_title(text)
                if title:
                    return f"Upstream provider returned an HTML error page: {title} (HTTP {response.status_code})."
                return f"Upstream provider returned an HTML error page (HTTP {response.status_code})."
            return text
        return f"HTTP {response.status_code}"

    def _looks_like_html_document(self, text: str) -> bool:
        normalized = text.lstrip().lower()
        return normalized.startswith("<!doctype html") or normalized.startswith("<html")

    def _extract_html_title(self, text: str) -> str | None:
        match = self._HTML_TITLE_RE.search(text)
        if not match:
            return None
        title = re.sub(r"\s+", " ", match.group("title")).strip()
        return title or None

    def _should_retry_without_images(self, error_message: str) -> bool:
        normalized = error_message.lower()
        keywords = [
            "image",
            "vision",
            "multimodal",
            "does not support",
            "unsupported content",
            "invalid content type",
            "content type",
            "input_image",
            "image_url",
            "expected a string",
            "must be a string",
            "got an array",
            "got array",
            "invalid type for",
        ]
        if any(keyword in normalized for keyword in keywords):
            return True
        return "messages[" in normalized and "content" in normalized

    def _parse_llm_answer(self, body: dict[str, object]) -> tuple[LLMAnswer, str | None]:
        try:
            choice = body["choices"][0]  # type: ignore[index]
            message = choice["message"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise DomainValidationError("LLM response format is invalid.") from exc

        # Tool-only responses (no content) are valid; return empty text answer.
        has_tool_calls = isinstance(message, dict) and bool(message.get("tool_calls"))

        parts = self._extract_content_parts(message)
        if not parts and not has_tool_calls:
            raise DomainValidationError("LLM returned an empty answer.")
        answer = "".join(part.text or "" for part in parts if part.type == "text")
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        return (
            LLMAnswer(
                answer=answer,
                content_parts=tuple(self._coalesce_content_parts(parts)),
            ),
            str(finish_reason).strip() if finish_reason is not None else None,
        )

    def _extract_content_parts(self, message: object) -> list[AssistantContentPart]:
        if isinstance(message, str):
            return self._extract_text_and_markdown_image_parts(message)

        if not isinstance(message, dict):
            return self._extract_content_parts_from_value(message)

        parts = self._extract_content_parts_from_value(message.get("content"))
        direct_image = self._extract_image_part_from_item(message)
        if direct_image is not None:
            parts.append(direct_image)
        return parts

    def _extract_content_parts_from_value(self, content: object) -> list[AssistantContentPart]:
        if isinstance(content, str):
            return self._extract_text_and_markdown_image_parts(content)
        if isinstance(content, dict):
            return self._extract_content_parts_from_items([content])
        if isinstance(content, list):
            return self._extract_content_parts_from_items(content)
        return []

    def _extract_content_parts_from_items(self, items: list[object]) -> list[AssistantContentPart]:
        parts: list[AssistantContentPart] = []
        for item in items:
            if isinstance(item, str):
                parts.extend(self._extract_text_and_markdown_image_parts(item))
                continue
            if not isinstance(item, dict):
                continue
            text_parts = self._extract_text_parts_from_item(item)
            if text_parts:
                parts.extend(text_parts)
                continue
            image_part = self._extract_image_part_from_item(item)
            if image_part is not None:
                parts.append(image_part)
        return parts

    def _extract_text_parts_from_item(self, item: dict[str, object]) -> list[AssistantContentPart]:
        part_type = str(item.get("type", "")).strip().lower()
        text_value = item.get("text")
        if part_type in {"text", "output_text"} and isinstance(text_value, str) and text_value:
            return self._extract_text_and_markdown_image_parts(text_value)
        if isinstance(text_value, str) and text_value and not part_type.startswith("image"):
            return self._extract_text_and_markdown_image_parts(text_value)
        return []

    def _extract_image_part_from_item(self, item: dict[str, object]) -> AssistantContentPart | None:
        part_type = str(item.get("type", "")).strip().lower()
        allowed_types = {
            "image",
            "image_url",
            "input_image",
            "output_image",
            "generated_image",
        }

        image_url = self._coerce_image_url(item)
        if image_url is None and part_type and part_type not in allowed_types:
            return None
        if image_url is None:
            return None

        original_url = image_url if image_url.startswith("http://") or image_url.startswith("https://") else None
        mime_type = self._coerce_image_mime_type(item, image_url)
        image_url, mime_type = self._materialize_image_url(
            image_url=image_url,
            mime_type=mime_type,
        )
        alt = self._coerce_optional_string(
            item.get("alt") or item.get("caption") or item.get("revised_prompt") or item.get("text")
        )
        return AssistantContentPart(
            type="image",
            url=image_url,
            original_url=original_url,
            mime_type=mime_type,
            alt=alt,
        )

    def _coerce_image_url(self, item: dict[str, object]) -> str | None:
        direct_url = self._coerce_optional_string(item.get("url"))
        if direct_url:
            return direct_url

        image_url = item.get("image_url")
        if isinstance(image_url, str) and image_url.strip():
            return image_url.strip()
        if isinstance(image_url, dict):
            nested_url = self._coerce_optional_string(image_url.get("url"))
            if nested_url:
                return nested_url

        b64_value = self._coerce_optional_string(
            item.get("b64_json") or item.get("image_base64") or item.get("base64")
        )
        if b64_value:
            mime_type = self._coerce_image_mime_type(item, None) or "image/png"
            return f"data:{mime_type};base64,{b64_value}"
        return None

    def _coerce_image_mime_type(self, item: dict[str, object], image_url: str | None) -> str | None:
        mime_type = self._normalize_image_mime_type(
            self._coerce_optional_string(item.get("mime_type") or item.get("media_type"))
        )
        if mime_type:
            return mime_type
        if image_url and image_url.startswith("data:image/"):
            prefix = image_url.split(";", 1)[0]
            return self._normalize_image_mime_type(prefix.removeprefix("data:"))
        return None

    def _coerce_optional_string(self, value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _extract_text_and_markdown_image_parts(self, text: str) -> list[AssistantContentPart]:
        parts: list[AssistantContentPart] = []
        cursor = 0

        for match in self._MARKDOWN_IMAGE_RE.finditer(text):
            start, end = match.span()
            if start > cursor:
                leading_text = text[cursor:start]
                if leading_text:
                    parts.append(AssistantContentPart(type="text", text=leading_text))

            image_part = self._build_markdown_image_part(
                alt=match.group("alt"),
                raw_url=match.group("url"),
            )
            if image_part is not None:
                parts.append(image_part)
            else:
                parts.append(AssistantContentPart(type="text", text=match.group(0)))
            cursor = end

        if cursor < len(text):
            trailing_text = text[cursor:]
            if trailing_text:
                parts.append(AssistantContentPart(type="text", text=trailing_text))

        if parts:
            return parts
        return [AssistantContentPart(type="text", text=text)] if text else []

    def _build_markdown_image_part(self, *, alt: str, raw_url: str) -> AssistantContentPart | None:
        image_url = raw_url.strip()
        if not image_url:
            return None

        if not self._looks_like_image_url(image_url):
            return None

        mime_type = self._coerce_image_mime_type({}, image_url)
        original_url = image_url if image_url.startswith("http://") or image_url.startswith("https://") else None
        image_url, mime_type = self._materialize_image_url(
            image_url=image_url,
            mime_type=mime_type,
        )
        return AssistantContentPart(
            type="image",
            url=image_url,
            original_url=original_url,
            mime_type=mime_type,
            alt=alt.strip() or None,
        )

    def _looks_like_image_url(self, image_url: str) -> bool:
        normalized = image_url.strip()
        if normalized.startswith("data:image/"):
            return True
        if normalized.startswith("http://") or normalized.startswith("https://"):
            suffix = PurePosixPath(urlparse(normalized).path).suffix.lower()
            return suffix in self._IMAGE_MIME_BY_SUFFIX or "image" in normalized.lower()
        return False

    def _resolve_request_timeout(self, *, user_prompt: str) -> float:
        default_timeout = float(self.settings.llm_timeout_seconds)
        if self._looks_like_image_generation_request(user_prompt):
            return max(default_timeout, self._IMAGE_GENERATION_TIMEOUT_SECONDS)
        return default_timeout

    def _looks_like_image_generation_request(self, user_prompt: str) -> bool:
        normalized = user_prompt.strip().lower()
        if not normalized:
            return False

        keywords = [
            "generate image",
            "generate an image",
            "create image",
            "create an image",
            "draw ",
            "illustration",
            "poster",
            "logo",
            "diagram",
            "生成图片",
            "生成一张图",
            "生成图像",
            "画一张",
            "画个",
            "绘制",
            "海报",
            "插画",
            "配图",
            "图片",
            "图像",
            "示意图",
            "流程图",
        ]
        return any(keyword in normalized for keyword in keywords)

    def _materialize_image_url(self, *, image_url: str, mime_type: str | None) -> tuple[str, str | None]:
        normalized_mime_type = self._normalize_image_mime_type(mime_type)
        if image_url.startswith("http://") or image_url.startswith("https://"):
            persisted = self._persist_remote_image_url(
                image_url=image_url,
                mime_type=normalized_mime_type,
            )
            if persisted is not None:
                return persisted
        return image_url, normalized_mime_type

    def _persist_remote_image_url(self, *, image_url: str, mime_type: str | None) -> tuple[str, str] | None:
        try:
            response = httpx.get(
                image_url,
                follow_redirects=True,
                timeout=self.settings.llm_timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        file_bytes = response.content
        if not file_bytes:
            return None

        max_size = max(1, int(self.settings.upload_max_file_size_mb)) * 1024 * 1024
        if len(file_bytes) > max_size:
            return None

        response_mime_type = self._normalize_image_mime_type(response.headers.get("content-type"))
        resolved_mime_type = (
            self._sniff_image_mime_type(file_bytes)
            or response_mime_type
            or mime_type
            or self._infer_image_mime_type_from_url(image_url)
        )
        if resolved_mime_type is None:
            return None

        return self._to_data_url(mime_type=resolved_mime_type, file_bytes=file_bytes), resolved_mime_type

    def _normalize_image_mime_type(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.split(";", 1)[0].strip().lower()
        if normalized.startswith("image/"):
            return normalized
        return None

    def _sniff_image_mime_type(self, file_bytes: bytes) -> str | None:
        if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if file_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
            return "image/webp"
        if file_bytes[:6] in {b"GIF87a", b"GIF89a"}:
            return "image/gif"
        return None

    def _infer_image_mime_type_from_url(self, image_url: str) -> str | None:
        suffix = PurePosixPath(urlparse(image_url).path).suffix.lower()
        return self._IMAGE_MIME_BY_SUFFIX.get(suffix)

    def _to_data_url(self, *, mime_type: str, file_bytes: bytes) -> str:
        encoded = base64.b64encode(file_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _coalesce_content_parts(self, parts: list[AssistantContentPart]) -> list[AssistantContentPart]:
        compacted: list[AssistantContentPart] = []
        for part in parts:
            if part.type == "text":
                if not part.text:
                    continue
                if compacted and compacted[-1].type == "text":
                    previous = compacted[-1]
                    compacted[-1] = AssistantContentPart(
                        type="text",
                        text=f"{previous.text or ''}{part.text}",
                    )
                    continue
                compacted.append(part)
                continue
            if part.type == "image" and part.url:
                compacted.append(part)
        return compacted

    def _build_text_answer(self, answer: str) -> LLMAnswer:
        return LLMAnswer(
            answer=answer,
            content_parts=(AssistantContentPart(type="text", text=answer),),
        )

    def _build_context(self, hits: list[dict[str, object]]) -> str:
        if not hits:
            return "(no context)"

        lines: list[str] = []
        for idx, hit in enumerate(hits, start=1):
            doc_id = str(hit.get("document_id", ""))
            chunk_index = hit.get("chunk_index", "")
            content = str(hit.get("content", "")).strip()
            lines.append(f"[{idx}] doc={doc_id} chunk={chunk_index}: {content}")

        return "\n".join(lines)

    def _extract_candidate_sentences(self, hits: list[dict[str, object]]) -> list[str]:
        candidates: list[str] = []
        for hit in hits[:3]:
            raw = str(hit.get("content", "")).strip()
            if not raw:
                continue

            cleaned = re.sub(r"(?m)^#+\s*.*$", " ", raw)
            normalized = re.sub(r"\s+", " ", cleaned)
            parts = re.split(r"(?<=[.!?])\s+", normalized)
            for part in parts:
                sentence = part.strip(" -\t\r\n")
                if sentence:
                    candidates.append(sentence[:200])
        return candidates

    def _pick_best_sentence(self, question: str, candidates: list[str]) -> str:
        if not candidates:
            return ""

        q_tokens = set(re.findall(r"\w+", question.lower()))
        if not q_tokens:
            return candidates[0]

        best_sentence = candidates[0]
        best_score = -1
        for sentence in candidates:
            s_tokens = set(re.findall(r"\w+", sentence.lower()))
            score = len(q_tokens.intersection(s_tokens))
            if score > best_score:
                best_score = score
                best_sentence = sentence

        return best_sentence
