from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.agent_run import AgentRun
from app.models.agent_step import AgentStep
from app.schemas.agent import (
    AgentRunRequest,
    AgentRunResponse,
    AgentRunStartResponse,
    AgentStepResponse,
    AgentStreamEvent,
    AgentToolCall,
)
from app.schemas.chat import ChatAskRequest, ChatSource
from app.services.chat_history_service import ChatHistoryService
from app.services.document_service import DocumentService
from app.services.llm_model_service import LLMModelService
from app.services.llm_router import LLMRouter
from app.services.llm_service import LLMCompletionResult, LLMService, LLMStreamChunk
from app.services.rag_chat_service import RagChatService
from app.services.tool_registry import get_openai_tools, get_tool_definition
from app.services.tool_service import ToolService
from app.services.user_service import UserService

_MAX_TOOL_LOOP_ITERATIONS = 15
_MAX_CONSECUTIVE_DUPLICATE_CALLS = 2


class AgentService:
    """Single-agent executor with LLM function-calling tool loop (Phase 1)."""

    _WORKFLOW_NODE_TYPES = {"llm", "retrieval", "tool_call", "condition", "final"}

    def __init__(
        self,
        *,
        db: Session,
        user_service: UserService,
        document_service: DocumentService,
        rag_chat_service: RagChatService,
        tool_service: ToolService,
        chat_history_service: ChatHistoryService,
        llm_service: LLMService,
        llm_model_service: LLMModelService,
    ) -> None:
        self.db = db
        self.user_service = user_service
        self.document_service = document_service
        self.rag_chat_service = rag_chat_service
        self.tool_service = tool_service
        self.chat_history_service = chat_history_service
        self.llm_service = llm_service
        self.llm_model_service = llm_model_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, payload: AgentRunRequest) -> AgentRunStartResponse:
        """Create a queued run, returning run_id and stream_url."""
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)

        run = self._create_queued_run(payload=payload, team_id=team_id, user_id=user_id)
        return AgentRunStartResponse(
            run_id=run.run_id,
            stream_url=f"/agent/runs/{run.run_id}/stream",
            status="queued",
        )

    def run(self, payload: AgentRunRequest) -> AgentRunResponse:
        """Synchronous run (backward-compatible)."""
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        effective_space_id = self.document_service.resolve_space_id(
            team_id=team_id,
            conversation_id=payload.conversation_id,
            space_id=payload.space_id,
            user_id=user_id,
        )
        started = time.perf_counter()
        run = self._create_run(payload=payload, team_id=team_id, user_id=user_id, space_id=effective_space_id)
        routes = self._resolve_routes(team_id=team_id, user_id=user_id, payload=payload)
        self._persist_routes(run, routes)

        if payload.run_mode.strip().lower() == "workflow" and self._workflow_nodes(payload.workflow_config):
            response = self._run_workflow(
                run=run, payload=payload, team_id=team_id, user_id=user_id,
                space_id=effective_space_id, started=started,
            )
        else:
            response = self._run_agent_auto_tool_loop(
                run=run, payload=payload, team_id=team_id, user_id=user_id,
                space_id=effective_space_id, started=started, routes=routes,
            )
        self._record_agent_history(payload=payload, response=response)
        return response

    def stream_run(self, run_id: str) -> Iterator[AgentStreamEvent]:
        """Execute run and yield SSE events. Loads context from saved request_payload_json."""
        run = self.db.get(AgentRun, run_id)
        if run is None:
            raise EntityNotFoundError(f"Agent run '{run_id}' not found.")
        if run.status != "queued":
            # Replay existing steps
            yield from self._replay_existing_run(run)
            return

        payload = self._load_request_payload(run)
        team_id = run.team_id
        user_id = run.user_id
        effective_space_id = self.document_service.resolve_space_id(
            team_id=team_id,
            conversation_id=run.conversation_id,
            space_id=run.space_id,
            user_id=user_id,
        )
        routes = self._resolve_routes(team_id=team_id, user_id=user_id, payload=payload)
        self._persist_routes(run, routes)

        seq = 0
        yield AgentStreamEvent(
            event="run.started", run_id=run.run_id, seq=seq,
            payload={"status": "running"},
        )

        started = time.perf_counter()
        try:
            if payload.run_mode.strip().lower() == "workflow" and self._workflow_nodes(payload.workflow_config):
                response = self._run_workflow(
                    run=run, payload=payload, team_id=team_id, user_id=user_id,
                    space_id=effective_space_id, started=started, stream_sink=True,
                )
            else:
                gen = self._run_agent_auto_tool_loop_stream(
                    run=run, payload=payload, team_id=team_id, user_id=user_id,
                    space_id=effective_space_id, started=started, routes=routes,
                )
                seq = None
                for event in gen:
                    seq = event.seq
                    yield event
                response = self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)
        except Exception as exc:
            self._finish_run(run=run, status="failed", final_answer=f"Agent execution failed: {exc}",
                             required_confirmations=[], started=started)
            seq += 1
            yield AgentStreamEvent(
                event="run.failed", run_id=run.run_id, seq=seq,
                payload={"error": str(exc)},
            )
            return

        seq = (seq or 0) + 1
        yield AgentStreamEvent(
            event="run.completed", run_id=run.run_id, seq=seq,
            payload=self._run_response_dict(response),
        )

    def confirm_run(self, *, run_id: str, team_id: str, user_id: str) -> AgentRunResponse:
        run = self._ensure_run_access(run_id=run_id, team_id=team_id, user_id=user_id)
        if run.status != "requires_confirmation":
            raise DomainValidationError("Only runs waiting for confirmation can be confirmed.")

        pending_calls = self._load_required_confirmations(run)
        if not pending_calls:
            raise DomainValidationError("This run has no pending tool calls.")

        loop_state = self._load_loop_state(run)
        if loop_state is None:
            # Keyword-fallback path: no LLM loop to resume, just execute and finish.
            return self._confirm_fallback(run=run, pending_calls=pending_calls,
                                          team_id=team_id, user_id=user_id)

        started = time.perf_counter()

        # Re-resolve real routes (DB persisted snapshot is masked; we need live keys).
        payload = self._load_request_payload(run)
        real_routes = self._resolve_routes(team_id=team_id, user_id=user_id, payload=payload)
        planner = self._routes_to_planner_dict(real_routes)

        # Execute the pending tool call
        next_index = self._next_step_index(run.run_id)
        tool_started = time.perf_counter()
        call = pending_calls[0]
        try:
            result = self.tool_service.execute(
                team_id=team_id,
                user_id=user_id,
                action=call.tool_name,
                arguments=call.arguments,
            )
            tool_result_entry = {"tool_name": call.tool_name, "arguments": call.arguments, "result": result}
            tool_status = "completed"
        except Exception as exc:  # noqa: BLE001
            tool_result_entry = {"tool_name": call.tool_name, "arguments": call.arguments, "error": str(exc)}
            tool_status = "failed"

        self._record_step(
            run=run, step_index=next_index, step_type="tool_call",
            title=f"Execute confirmed tool: {call.tool_name}", status=tool_status,
            tool_name=call.tool_name,
            input_payload={"calls": [call.model_dump()], "confirmed": True},
            output_payload={"executed": [tool_result_entry]},
            error_message=str(tool_result_entry.get("error")) if "error" in tool_result_entry else None,
            latency_ms=self._elapsed_ms(tool_started),
        )

        # Add tool result to messages and resume LLM loop.
        # The loop state already contains the assistant message with tool_calls
        # from when the loop was interrupted (see _save_loop_state).
        messages = list(loop_state.get("messages", []))
        pending_tool_id = loop_state.get("pending_tool_call_id", f"call_{tool_status}")
        messages.append({
            "role": "tool",
            "tool_call_id": pending_tool_id,
            "content": json.dumps(tool_result_entry, ensure_ascii=False, default=str),
        })

        tools = self._build_tools(payload=payload, run=run)

        step_index = next_index + 1
        final_answer, final_status, _ = self._tool_loop(
            run=run, messages=messages, tools=tools,
            team_id=team_id, user_id=user_id, space_id=run.space_id,
            base_url=planner["base_url"],
            api_key=planner["api_key"],
            model_name=planner["model_name"],
            step_index=step_index, started=started,
            enable_tool_execution=True, confirm_dangerous_actions=True, dry_run=False,
            enabled_tools=payload.enabled_tools,
        )

        self._record_step(
            run=run, step_index=step_index + 50, step_type="final",
            title="Summarize confirmed result", status=final_status,
            input_payload={"run_id": run_id},
            output_payload={"answer": final_answer, "status": final_status},
        )
        self._finish_run(
            run=run, status=final_status, final_answer=final_answer,
            required_confirmations=[], started=started,
        )
        return self.get_run(run_id=run_id, team_id=team_id, user_id=user_id)

    def _confirm_fallback(
        self,
        *,
        run: AgentRun,
        pending_calls: list[AgentToolCall],
        team_id: str,
        user_id: str,
    ) -> AgentRunResponse:
        """Execute pending dangerous tools without LLM loop resume (keyword-fallback path)."""
        started = time.perf_counter()
        next_index = self._next_step_index(run.run_id)

        tool_results: list[dict[str, object]] = []
        tool_errors: list[str] = []
        for call in pending_calls:
            try:
                result = self.tool_service.execute(
                    team_id=team_id, user_id=user_id,
                    action=call.tool_name, arguments=call.arguments,
                )
                tool_results.append({"tool_name": call.tool_name, "arguments": call.arguments, "result": result})
            except Exception as exc:  # noqa: BLE001
                tool_errors.append(f"{call.tool_name}: {exc}")

        status = "failed" if tool_errors and not tool_results else ("partial_success" if tool_errors else "completed")
        self._record_step(
            run=run, step_index=next_index, step_type="tool_call",
            title="Execute confirmed tools (fallback)", status=status,
            tool_name=",".join(call.tool_name for call in pending_calls),
            input_payload={"calls": [call.model_dump() for call in pending_calls], "confirmed": True},
            output_payload={"executed": tool_results, "errors": tool_errors},
            error_message="; ".join(tool_errors) or None,
            latency_ms=self._elapsed_ms(started),
        )
        final_answer = self._build_final_answer(
            status=status, task=run.task, rag_answer="",
            tool_results=tool_results, skipped_calls=[],
            tool_errors=tool_errors, dry_run=False, confirmed=True,
        )
        self._record_step(
            run=run, step_index=next_index + 1, step_type="final",
            title="Summarize confirmed result (fallback)", status=status,
            input_payload={"run_id": run.run_id},
            output_payload={"answer": final_answer, "status": status},
        )
        self._finish_run(
            run=run, status=status, final_answer=final_answer,
            required_confirmations=[], started=started,
        )
        return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

    def get_run(self, *, run_id: str, team_id: str, user_id: str) -> AgentRunResponse:
        run = self._ensure_run_access(run_id=run_id, team_id=team_id, user_id=user_id)
        steps = self._load_steps(run.run_id)
        return AgentRunResponse(
            run_id=run.run_id, team_id=run.team_id, user_id=run.user_id,
            conversation_id=run.conversation_id, space_id=run.space_id,
            app_id=run.app_id, app_version=run.app_version,
            trigger_channel=run.trigger_channel, task=run.task,
            status=run.status, answer=run.final_answer,
            model=run.model, dry_run=run.dry_run, max_steps=run.max_steps,
            required_confirmations=self._load_required_confirmations(run),
            steps=[AgentStepResponse.from_record(item) for item in steps],
            tool_calls=self._extract_tool_calls_from_steps(steps),
            sources=self._extract_sources_from_steps(steps),
            latency_ms=run.latency_ms, created_at=run.created_at,
            completed_at=run.completed_at,
            model_routes=self._load_routes_snapshot(run),
        )

    def list_tools(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self.tool_service.list_tools()]

    # ------------------------------------------------------------------
    # Core LLM tool loop (agent_auto)
    # ------------------------------------------------------------------

    def _run_agent_auto_tool_loop(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
        routes: dict[str, dict[str, str]],
    ) -> AgentRunResponse:
        """LLM function-calling tool loop as primary path; keyword matching as fallback."""
        try:
            return self._run_agent_auto_tool_loop_inner(
                run=run, payload=payload, team_id=team_id, user_id=user_id,
                space_id=space_id, started=started, routes=routes,
            )
        except DomainValidationError as exc:
            if self._should_fallback_to_keyword(str(exc)):
                return self._run_agent_auto_fallback(
                    run=run, payload=payload, team_id=team_id, user_id=user_id,
                    space_id=space_id, started=started,
                )
            raise

    def _run_agent_auto_tool_loop_inner(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
        routes: dict[str, object],
    ) -> AgentRunResponse:
        # Phase 1 plan step (trace)
        step_index = 1
        planner = self._routes_to_planner_dict(routes)
        model_name = planner["model_name"]
        self._record_step(
            run=run, step_index=step_index, step_type="plan",
            title="LLM function-calling plan",
            status="completed",
            input_payload={"task": payload.task, "max_steps": payload.max_steps, "run_mode": "agent_auto"},
            output_payload={"strategy": "llm_function_calling", "model": model_name},
        )

        # RAG retrieval as initial observation
        step_index += 1
        rag_answer, retrieval_failed = self._record_retrieval_step(
            run=run, payload=payload, team_id=team_id, user_id=user_id,
            space_id=space_id, step_index=step_index, title="Retrieve workspace context",
        )
        if retrieval_failed is not None:
            self._finish_run(run=run, status="failed",
                             final_answer=f"Agent 检索阶段失败：{retrieval_failed}",
                             required_confirmations=[], started=started)
            return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

        # Build messages
        messages = self._build_agent_messages(
            task=payload.task, system_prompt=payload.system_prompt,
            rag_answer=rag_answer,
        )
        tools = self._build_tools(payload=payload, run=run)

        step_index += 1
        final_answer, final_status, requires_confirmation = self._tool_loop(
            run=run, messages=messages, tools=tools,
            team_id=team_id, user_id=user_id, space_id=space_id,
            base_url=planner["base_url"],
            api_key=planner["api_key"],
            model_name=planner["model_name"],
            step_index=step_index, started=started,
            enable_tool_execution=not payload.dry_run,
            confirm_dangerous_actions=payload.confirm_dangerous_actions,
            dry_run=payload.dry_run,
            enabled_tools=payload.enabled_tools,
        )

        if requires_confirmation:
            pending = self._load_pending_tool_call(run)
            self._finish_run(
                run=run, status="requires_confirmation",
                final_answer=final_answer,
                required_confirmations=[pending] if pending else [],
                started=started,
            )
            return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

        self._record_step(
            run=run, step_index=step_index + 50, step_type="final",
            title="Summarize result", status=final_status,
            input_payload={"task": payload.task},
            output_payload={"answer": final_answer, "status": final_status},
        )
        self._finish_run(run=run, status=final_status, final_answer=final_answer,
                         required_confirmations=[], started=started)
        return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

    def _run_agent_auto_tool_loop_stream(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
        routes: dict[str, object],
    ) -> Iterator[AgentStreamEvent]:
        """Streaming variant of the LLM tool loop."""
        seq = 1
        planner = self._routes_to_planner_dict(routes)
        model_name = planner["model_name"]

        yield AgentStreamEvent(
            event="step.started", run_id=run.run_id, step_index=1, seq=seq,
            payload={"step_type": "plan", "title": "LLM function-calling plan"},
        )
        self._record_step(
            run=run, step_index=1, step_type="plan", title="LLM function-calling plan",
            status="completed",
            input_payload={"task": payload.task, "max_steps": payload.max_steps, "run_mode": "agent_auto"},
            output_payload={"strategy": "llm_function_calling", "model": model_name},
        )

        seq += 1
        yield AgentStreamEvent(
            event="step.started", run_id=run.run_id, step_index=2, seq=seq,
            payload={"step_type": "retrieval", "title": "Retrieve workspace context"},
        )
        rag_answer, retrieval_failed = self._record_retrieval_step(
            run=run, payload=payload, team_id=team_id, user_id=user_id,
            space_id=space_id, step_index=2, title="Retrieve workspace context",
        )
        if retrieval_failed is not None:
            self._finish_run(run=run, status="failed",
                             final_answer=f"Agent 检索阶段失败：{retrieval_failed}",
                             required_confirmations=[], started=started)
            yield AgentStreamEvent(
                event="run.failed", run_id=run.run_id, seq=seq,
                payload={"error": retrieval_failed},
            )
            return

        messages = self._build_agent_messages(
            task=payload.task, system_prompt=payload.system_prompt,
            rag_answer=rag_answer,
        )
        tools = self._build_tools(payload=payload, run=run)

        seq += 1
        yield from self._tool_loop_stream(
            run=run, messages=messages, tools=tools,
            team_id=team_id, user_id=user_id, space_id=space_id,
            base_url=planner["base_url"],
            api_key=planner["api_key"],
            model_name=planner["model_name"],
            started=started, start_seq=seq,
            enable_tool_execution=not payload.dry_run,
            confirm_dangerous_actions=payload.confirm_dangerous_actions,
            dry_run=payload.dry_run,
            enabled_tools=payload.enabled_tools,
        )

    # ------------------------------------------------------------------
    # Tool loop (shared between sync and streaming)
    # ------------------------------------------------------------------

    def _tool_loop(
        self,
        *,
        run: AgentRun,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        team_id: str,
        user_id: str,
        space_id: str | None,
        base_url: str,
        api_key: str,
        model_name: str,
        step_index: int,
        started: float,
        enable_tool_execution: bool,
        confirm_dangerous_actions: bool,
        dry_run: bool,
        enabled_tools: list[str] | None = None,
    ) -> tuple[str, str, bool]:
        """Run the LLM tool loop synchronously.

        Returns (final_answer, status, requires_confirmation).
        """
        tool_call_history: list[tuple[str, str]] = []
        accumulated_text: list[str] = []
        pending_confirmation = False
        current_step = step_index

        for iteration in range(_MAX_TOOL_LOOP_ITERATIONS):
            result = self.llm_service.complete_chat(
                messages=messages,
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                tools=tools if tools else None,
            )

            if result.assistant_text:
                accumulated_text.append(result.assistant_text)

            if not result.tool_calls:
                # No more tool calls — LLM is done
                final_text = "\n".join(accumulated_text).strip()
                if not final_text:
                    final_text = self._build_final_answer(
                        status="completed", task=run.task, rag_answer="",
                        tool_results=[], skipped_calls=[], tool_errors=[], dry_run=dry_run,
                    )
                return final_text, "completed", False

            # Process tool calls
            for tc in result.tool_calls:
                fn = tc.get("function", {})
                tool_name = str(fn.get("name", "")).strip()
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except (TypeError, ValueError):
                        raw_args = {}
                args = raw_args if isinstance(raw_args, dict) else {}

                # Duplicate guard
                call_sig = self._tool_call_signature(tool_name, args)
                if self._count_consecutive(tool_call_history, call_sig) >= _MAX_CONSECUTIVE_DUPLICATE_CALLS:
                    accumulated_text.append(f"\n[Tool call loop stopped: repeated {tool_name} {_MAX_CONSECUTIVE_DUPLICATE_CALLS}+ times.]")
                    final_text = "\n".join(accumulated_text).strip()
                    return final_text, "completed", False

                # Validate tool exists
                definition = get_tool_definition(tool_name)
                if definition is None:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "unknown")),
                        "content": json.dumps({"error": f"Unknown tool: {tool_name}"}),
                    })
                    continue

                # Validate arguments
                normalized_args = definition.normalize_arguments(args)
                arg_errors = definition.validate_arguments(normalized_args)
                if arg_errors:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "unknown")),
                        "content": json.dumps({"error": f"Argument validation failed: {'; '.join(arg_errors)}"}),
                    })
                    continue

                # Filter by enabled_tools
                if enabled_tools and tool_name not in enabled_tools:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "unknown")),
                        "content": json.dumps({"error": f"Tool '{tool_name}' is not enabled for this run."}),
                    })
                    continue

                # Check dangerous + confirmation
                if definition.dangerous and not dry_run and not confirm_dangerous_actions:
                    pending_confirmation = True
                    call_obj = AgentToolCall(
                        tool_name=tool_name, arguments=normalized_args,
                        dangerous=True, requires_confirmation=True,
                    )
                    self._save_pending_tool_call(run, call_obj)
                    # Append assistant tool_calls message so confirm can resume the loop.
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    self._save_loop_state(run, messages=messages, pending_tool_call_id=str(tc.get("id", "unknown")))
                    final_text = "\n".join(accumulated_text).strip()
                    return final_text, "requires_confirmation", True

                # Record step
                current_step += 1
                tool_started = time.perf_counter()
                if dry_run:
                    tool_result = {"tool_name": tool_name, "arguments": normalized_args, "result": {"dry_run": True}}
                    tool_status = "skipped"
                else:
                    try:
                        exec_result = self.tool_service.execute(
                            team_id=team_id, user_id=user_id,
                            action=tool_name, arguments=normalized_args,
                        )
                        tool_result = {"tool_name": tool_name, "arguments": normalized_args, "result": exec_result}
                        tool_status = "completed"
                    except Exception as exc:  # noqa: BLE001
                        tool_result = {"tool_name": tool_name, "arguments": normalized_args, "error": str(exc)}
                        tool_status = "failed"

                self._record_step(
                    run=run, step_index=current_step, step_type="tool_call",
                    title=f"Execute: {tool_name}", status=tool_status,
                    tool_name=tool_name,
                    input_payload={"calls": [{"tool_name": tool_name, "arguments": normalized_args}]},
                    output_payload={"executed": [tool_result], "errors": [str(tool_result["error"])] if "error" in tool_result else []},
                    error_message=str(tool_result.get("error")) if "error" in tool_result else None,
                    latency_ms=self._elapsed_ms(tool_started),
                )
                tool_call_history.append(call_sig)

                # Append tool result to messages
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(tc.get("id", "unknown")),
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

        # Max iterations reached
        final_text = "\n".join(accumulated_text).strip()
        if not final_text:
            final_text = "Agent has run the maximum number of tool loop iterations."
        return final_text, "completed", False

    def _tool_loop_stream(
        self,
        *,
        run: AgentRun,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        team_id: str,
        user_id: str,
        space_id: str | None,
        base_url: str,
        api_key: str,
        model_name: str,
        started: float,
        start_seq: int,
        enable_tool_execution: bool,
        confirm_dangerous_actions: bool,
        dry_run: bool,
        enabled_tools: list[str] | None = None,
    ) -> Iterator[AgentStreamEvent]:
        """Streaming variant of the LLM tool loop."""
        tool_call_history: list[tuple[str, str]] = []
        accumulated_text: list[str] = []
        seq = start_seq
        current_step = 3

        for iteration in range(_MAX_TOOL_LOOP_ITERATIONS):
            accumulated_names: dict[str, str] = {}
            accumulated_args: dict[str, str] = {}
            ordered_indices: dict[str, int] = {}
            current_tool_calls: list[dict[str, object]] = []

            try:
                for chunk in self.llm_service.stream_chat(
                    messages=messages,
                    model=model_name,
                    base_url=base_url,
                    api_key=api_key,
                    tools=tools if tools else None,
                ):
                    if chunk.delta_text:
                        accumulated_text.append(chunk.delta_text)
                        seq += 1
                        yield AgentStreamEvent(
                            event="llm.delta", run_id=run.run_id, step_index=current_step, seq=seq,
                            payload={"text": chunk.delta_text},
                        )
                    for td in chunk.tool_call_deltas:
                        idx = str(td.get("index", ""))
                        if idx not in ordered_indices:
                            ordered_indices[idx] = len(ordered_indices)
                        fn = td.get("function", {})
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                accumulated_names[idx] = str(fn["name"])
                            if fn.get("arguments"):
                                accumulated_args[idx] = accumulated_args.get(idx, "") + str(fn["arguments"])
            except DomainValidationError as exc:
                if self._should_fallback_to_keyword(str(exc)):
                    fallback_text = self._run_agent_auto_fallback_sync(
                        run=run, team_id=team_id, user_id=user_id,
                        space_id=space_id, started=started,
                    )
                    seq += 1
                    yield AgentStreamEvent(
                        event="llm.delta", run_id=run.run_id, seq=seq,
                        payload={"text": fallback_text},
                    )
                    return
                raise

            # Build tool calls from accumulated deltas
            current_tool_calls = self.llm_service.finalize_tool_calls_from_stream(
                accumulated_names=accumulated_names,
                accumulated_args=accumulated_args,
                ordered_indices=ordered_indices,
            )

            if not current_tool_calls:
                break

            for tc in current_tool_calls:
                fn = tc.get("function", {})
                tool_name = str(fn.get("name", "")).strip()
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (TypeError, ValueError):
                        args = {}
                if not isinstance(args, dict):
                    args = {}

                seq += 1
                yield AgentStreamEvent(
                    event="tool.proposed", run_id=run.run_id, step_index=current_step, seq=seq,
                    payload={"tool_name": tool_name, "arguments": args},
                )

                # Duplicate guard
                call_sig = self._tool_call_signature(tool_name, args)
                if self._count_consecutive(tool_call_history, call_sig) >= _MAX_CONSECUTIVE_DUPLICATE_CALLS:
                    break

                definition = get_tool_definition(tool_name)
                if definition is None:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "unknown")),
                        "content": json.dumps({"error": f"Unknown tool: {tool_name}"}),
                    })
                    continue

                normalized_args = definition.normalize_arguments(args)
                if definition.validate_arguments(normalized_args):
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "unknown")),
                        "content": json.dumps({"error": f"Argument validation failed for {tool_name}."}),
                    })
                    continue
                enabled_tool_names = {t.strip().lower() for t in (enabled_tools or []) if t.strip()} if enabled_tools else None
                if enabled_tool_names and tool_name.lower() not in enabled_tool_names:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "unknown")),
                        "content": json.dumps({"error": f"Tool '{tool_name}' is not enabled for this run."}),
                    })
                    continue

                if definition.dangerous and not dry_run and not confirm_dangerous_actions:
                    pending = AgentToolCall(
                        tool_name=tool_name, arguments=normalized_args,
                        dangerous=True, requires_confirmation=True,
                    )
                    self._save_pending_tool_call(run, pending)
                    # Append assistant tool_calls so confirm can resume the loop properly.
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    self._save_loop_state(run, messages=messages, pending_tool_call_id=str(tc.get("id", "unknown")))
                    # Persist the requires_confirmation status so confirm_run can find it.
                    self._finish_run(
                        run=run, status="requires_confirmation",
                        final_answer="\n".join(accumulated_text).strip() or "Agent requires confirmation to proceed.",
                        required_confirmations=[pending], started=started,
                    )

                    seq += 1
                    yield AgentStreamEvent(
                        event="confirmation.required", run_id=run.run_id, step_index=current_step, seq=seq,
                        payload={"tool_name": tool_name, "arguments": normalized_args},
                    )
                    return

                current_step += 1
                seq += 1
                yield AgentStreamEvent(
                    event="tool.started", run_id=run.run_id, step_index=current_step, seq=seq,
                    payload={"tool_name": tool_name},
                )

                tool_started = time.perf_counter()
                if dry_run:
                    tool_result = {"tool_name": tool_name, "arguments": normalized_args, "result": {"dry_run": True}}
                    tool_status = "skipped"
                else:
                    try:
                        exec_result = self.tool_service.execute(
                            team_id=team_id, user_id=user_id,
                            action=tool_name, arguments=normalized_args,
                        )
                        tool_result = {"tool_name": tool_name, "arguments": normalized_args, "result": exec_result}
                        tool_status = "completed"
                    except Exception as exc:  # noqa: BLE001
                        tool_result = {"tool_name": tool_name, "arguments": normalized_args, "error": str(exc)}
                        tool_status = "failed"

                self._record_step(
                    run=run, step_index=current_step, step_type="tool_call",
                    title=f"Execute: {tool_name}", status=tool_status,
                    tool_name=tool_name,
                    input_payload={"calls": [{"tool_name": tool_name, "arguments": normalized_args}]},
                    output_payload={"executed": [tool_result], "errors": [str(tool_result["error"])] if "error" in tool_result else []},
                    error_message=str(tool_result.get("error")) if "error" in tool_result else None,
                    latency_ms=self._elapsed_ms(tool_started),
                )
                tool_call_history.append(call_sig)

                seq += 1
                yield AgentStreamEvent(
                    event="tool.result", run_id=run.run_id, step_index=current_step, seq=seq,
                    payload={"tool_name": tool_name, "result": tool_result},
                )

                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(tc.get("id", "unknown")),
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

            if not current_tool_calls or tool_call_history and self._count_consecutive(
                tool_call_history, call_sig
            ) >= _MAX_CONSECUTIVE_DUPLICATE_CALLS:
                break

        final_text = "\n".join(accumulated_text).strip()
        if not final_text:
            final_text = "Agent has completed its analysis."
        self._record_step(
            run=run, step_index=50, step_type="final", title="Summarize result",
            status="completed",
            input_payload={"run_id": run.run_id},
            output_payload={"answer": final_text, "status": "completed"},
        )
        self._finish_run(run=run, status="completed", final_answer=final_text,
                         required_confirmations=[], started=started)
        seq += 1
        yield AgentStreamEvent(
            event="step.completed", run_id=run.run_id, step_index=50, seq=seq,
            payload={"answer": final_text},
        )

    # ------------------------------------------------------------------
    # Fallback: keyword-based planning
    # ------------------------------------------------------------------

    def _should_fallback_to_keyword(self, error_message: str) -> bool:
        normalized = error_message.lower()
        return any(kw in normalized for kw in [
            "llm_tools_unsupported", "llm_stream_unsupported",
            "tools", "tool_choice", "function calling",
        ])

    def _run_agent_auto_fallback(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
    ) -> AgentRunResponse:
        """Original keyword-matching agent_auto path (kept for backward compat)."""
        planned_calls = self._plan_tool_calls(payload.task, enabled_tools=payload.enabled_tools, space_id=space_id)
        step_index = 1
        self._record_step(
            run=run, step_index=step_index, step_type="plan", title="Build execution plan (fallback)",
            status="completed",
            input_payload={"task": payload.task, "strategy": "keyword_fallback"},
            output_payload={"planned_tool_calls": [item.model_dump() for item in planned_calls]},
        )

        step_index += 1
        rag_answer, retrieval_failed = self._record_retrieval_step(
            run=run, payload=payload, team_id=team_id, user_id=user_id,
            space_id=space_id, step_index=step_index, title="Retrieve workspace context",
        )
        if retrieval_failed is not None:
            self._finish_run(run=run, status="failed",
                             final_answer=f"Agent 检索阶段失败：{retrieval_failed}",
                             required_confirmations=[], started=started)
            return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

        tool_results, skipped_calls, tool_errors = [], [], []
        if planned_calls:
            step_index += 1
            tool_started = time.perf_counter()
            tool_results, skipped_calls, tool_errors, tool_status = self._execute_tool_calls(
                calls=planned_calls, team_id=team_id, user_id=user_id,
                space_id=space_id, task=payload.task,
                dry_run=payload.dry_run,
                confirm_dangerous_actions=payload.confirm_dangerous_actions,
            )
            self._record_step(
                run=run, step_index=step_index, step_type="tool_call",
                title="Execute selected tools", status=tool_status,
                tool_name=",".join(call.tool_name for call in planned_calls),
                input_payload={"calls": [call.model_dump() for call in planned_calls]},
                output_payload={"executed": tool_results, "skipped": [c.model_dump() for c in skipped_calls], "errors": tool_errors},
                error_message="; ".join(tool_errors) or None,
                latency_ms=self._elapsed_ms(tool_started),
            )

        step_index += 1
        status = self._resolve_run_status(dry_run=payload.dry_run, skipped_calls=skipped_calls,
                                           tool_errors=tool_errors, tool_results=tool_results)
        final_answer = self._build_final_answer(
            status=status, task=payload.task, rag_answer=rag_answer,
            tool_results=tool_results, skipped_calls=skipped_calls,
            tool_errors=tool_errors, dry_run=payload.dry_run,
        )
        self._record_step(
            run=run, step_index=step_index, step_type="final", title="Summarize result",
            status=status, input_payload={"task": payload.task},
            output_payload={"answer": final_answer, "status": status},
        )
        self._finish_run(run=run, status=status, final_answer=final_answer,
                         required_confirmations=skipped_calls, started=started)
        return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

    def _run_agent_auto_fallback_sync(
        self,
        *,
        run: AgentRun,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
    ) -> str:
        """Run keyword fallback, persist final state, return final answer text."""
        payload = self._load_request_payload(run)
        planned_calls = self._plan_tool_calls(run.task, enabled_tools=payload.enabled_tools, space_id=space_id)
        tool_results, skipped_calls, tool_errors = [], [], []
        if planned_calls:
            tool_results, skipped_calls, tool_errors, _ = self._execute_tool_calls(
                calls=planned_calls, team_id=team_id, user_id=user_id,
                space_id=space_id, task=run.task,
                dry_run=run.dry_run, confirm_dangerous_actions=False,
            )
        status = self._resolve_run_status(
            dry_run=run.dry_run, skipped_calls=skipped_calls,
            tool_errors=tool_errors, tool_results=tool_results,
        )
        final_answer = self._build_final_answer(
            status=status, task=run.task, rag_answer="",
            tool_results=tool_results, skipped_calls=skipped_calls,
            tool_errors=tool_errors, dry_run=run.dry_run,
        )
        # Persist: record final step and finish the run.
        self._record_step(
            run=run, step_index=50, step_type="final", title="Summarize result (fallback)",
            status=status, input_payload={"run_id": run.run_id},
            output_payload={"answer": final_answer, "status": status},
        )
        self._finish_run(run=run, status=status, final_answer=final_answer,
                         required_confirmations=skipped_calls, started=started)
        return final_answer

    # ------------------------------------------------------------------
    # Workflow mode (unchanged from original, but accepts optional stream_sink)
    # ------------------------------------------------------------------

    def _run_workflow(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
        stream_sink: bool = False,
    ) -> AgentRunResponse:
        nodes = self._workflow_nodes(payload.workflow_config)
        self._record_step(
            run=run, step_index=1, step_type="plan", title="Load workflow plan",
            status="completed",
            input_payload={"task": payload.task, "max_steps": payload.max_steps},
            output_payload={"workflow_nodes": nodes[: payload.max_steps]},
        )

        rag_answer = ""
        tool_results: list[dict[str, object]] = []
        skipped_calls: list[AgentToolCall] = []
        tool_errors: list[str] = []
        step_index = 1

        for node in nodes[: max(1, payload.max_steps - 1)]:
            node_type = str(node.get("type", "")).strip().lower()
            title = str(node.get("title") or node.get("id") or node_type or "Workflow node").strip()
            if node_type not in self._WORKFLOW_NODE_TYPES:
                tool_errors.append(f"{title}: unsupported workflow node type '{node_type}'.")
                continue
            if skipped_calls:
                break

            step_index += 1
            if node_type == "retrieval":
                answer, error = self._record_retrieval_step(
                    run=run, payload=payload, team_id=team_id, user_id=user_id,
                    space_id=space_id, step_index=step_index, title=title,
                )
                if error is not None:
                    tool_errors.append(f"{title}: {error}")
                else:
                    rag_answer = answer
                continue

            if node_type == "tool_call":
                try:
                    call = self._tool_call_from_workflow_node(node=node, space_id=space_id, task=payload.task)
                except DomainValidationError as exc:
                    tool_errors.append(f"{title}: {exc}")
                    continue
                call_results, call_skipped, call_errors, tool_status = self._execute_tool_calls(
                    calls=[call], team_id=team_id, user_id=user_id,
                    space_id=space_id, task=payload.task,
                    dry_run=payload.dry_run,
                    confirm_dangerous_actions=payload.confirm_dangerous_actions,
                )
                tool_results.extend(call_results)
                skipped_calls.extend(call_skipped)
                tool_errors.extend(call_errors)
                self._record_step(
                    run=run, step_index=step_index, step_type="tool_call",
                    title=title, status=tool_status, tool_name=call.tool_name,
                    input_payload={"calls": [call.model_dump()]},
                    output_payload={"executed": call_results, "skipped": [c.model_dump() for c in call_skipped], "errors": call_errors},
                    error_message="; ".join(call_errors) or None,
                )
                continue

            if node_type in {"condition", "llm"}:
                self._record_step(
                    run=run, step_index=step_index, step_type="observation",
                    title=title, status="completed",
                    input_payload=node,
                    output_payload={"note": "Lightweight MVP records this node for trace and continues sequentially."},
                )
                continue

            if node_type == "final":
                self._record_step(
                    run=run, step_index=step_index, step_type="observation",
                    title=title, status="completed",
                    input_payload=node,
                    output_payload={"note": "Final node reached."},
                )
                break

        final_status = self._resolve_run_status(
            dry_run=payload.dry_run, skipped_calls=skipped_calls,
            tool_errors=tool_errors, tool_results=tool_results,
        )
        final_answer = self._build_final_answer(
            status=final_status, task=payload.task, rag_answer=rag_answer,
            tool_results=tool_results, skipped_calls=skipped_calls,
            tool_errors=tool_errors, dry_run=payload.dry_run,
        )
        self._record_step(
            run=run, step_index=step_index + 1, step_type="final",
            title="Summarize workflow result", status=final_status,
            input_payload={"task": payload.task},
            output_payload={"answer": final_answer, "status": final_status},
        )
        self._finish_run(run=run, status=final_status, final_answer=final_answer,
                         required_confirmations=skipped_calls, started=started)
        return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

    # ------------------------------------------------------------------
    # Helpers: retrieval, tool execution, build messages, routing
    # ------------------------------------------------------------------

    def _build_agent_messages(
        self,
        *,
        task: str,
        system_prompt: str | None,
        rag_answer: str,
    ) -> list[dict[str, object]]:
        system = (system_prompt or "").strip()
        if not system:
            system = "You are CaiBao, an enterprise operations agent. Use tools when appropriate and answer based on available data."

        messages: list[dict[str, object]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        if rag_answer.strip():
            messages.insert(1, {
                "role": "assistant",
                "content": f"Retrieved workspace context: {rag_answer[:2000]}",
            })
        return messages

    def _build_tools(
        self,
        *,
        payload: AgentRunRequest | None,
        run: AgentRun,
    ) -> list[dict[str, object]]:
        enabled_tools = payload.enabled_tools if payload is not None else None
        return get_openai_tools(enabled_tools=enabled_tools)

    def _resolve_routes(
        self,
        *,
        team_id: str,
        user_id: str,
        payload: AgentRunRequest,
    ) -> dict[str, object]:
        """Resolve LLM routes; returns dict[str, ModelRoute] with real keys."""
        router = LLMRouter(
            db=self.db,
            user_id=user_id,
            team_id=team_id,
            llm_model_service=self.llm_model_service,
        )
        return router.resolve(
            explicit_model=payload.model,
            explicit_routes=payload.model_routes,
        )

    @staticmethod
    def _routes_to_planner_dict(routes: dict[str, object]) -> dict[str, str]:
        """Extract {base_url, api_key, model_name} from a ModelRoute planner for _tool_loop."""
        planner = routes.get("planner")
        if planner is None:
            return {"base_url": "", "api_key": "", "model_name": ""}
        if hasattr(planner, "base_url"):
            return {
                "base_url": getattr(planner, "base_url", ""),
                "api_key": (getattr(planner, "api_key", None) or ""),
                "model_name": getattr(planner, "model_name", ""),
            }
        if isinstance(planner, dict):
            return {
                "base_url": str(planner.get("base_url", "")),
                "api_key": str(planner.get("api_key", "") or ""),
                "model_name": str(planner.get("model_name", "")),
            }
        return {"base_url": "", "api_key": "", "model_name": ""}

    def _persist_routes(self, run: AgentRun, routes: dict[str, object]) -> None:
        """Persist masked routes snapshot (no raw API keys) to DB."""
        snapshot = LLMRouter.routes_snapshot(routes)
        run.model_routes_json = self._to_json(snapshot)
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

    def _load_routes(self, run: AgentRun) -> dict[str, dict[str, str]] | None:
        if not run.model_routes_json:
            return None
        return self._safe_json_dict(run.model_routes_json)

    def _load_routes_snapshot(self, run: AgentRun) -> dict[str, Any] | None:
        return self._load_routes(run)

    def _tool_call_signature(self, tool_name: str, arguments: dict[str, object]) -> tuple[str, str]:
        sorted_args = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
        return (tool_name.strip().lower(), sorted_args)

    def _count_consecutive(self, history: list[tuple[str, str]], sig: tuple[str, str]) -> int:
        count = 0
        for item in reversed(history):
            if item == sig:
                count += 1
            else:
                break
        return count

    def _save_loop_state(self, run: AgentRun, *, messages: list[dict[str, object]], pending_tool_call_id: str) -> None:
        loop = {
            "messages": self._strip_non_serializable_messages(messages),
            "pending_tool_call_id": pending_tool_call_id,
        }
        run.loop_state_json = self._to_json(loop)
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

    def _load_loop_state(self, run: AgentRun) -> dict[str, Any] | None:
        if not run.loop_state_json:
            return None
        return self._safe_json_dict(run.loop_state_json)

    def _save_pending_tool_call(self, run: AgentRun, call: AgentToolCall) -> None:
        run.required_confirmations_json = self._to_json([call.model_dump()])
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

    def _load_pending_tool_call(self, run: AgentRun) -> AgentToolCall | None:
        calls = self._load_required_confirmations(run)
        return calls[0] if calls else None

    def _strip_non_serializable_messages(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        cleaned: list[dict[str, object]] = []
        for msg in messages:
            c = dict(msg)
            content = c.get("content")
            if isinstance(content, list):
                c["content"] = json.dumps(content, ensure_ascii=False, default=str)
            if "tool_calls" in c and c["tool_calls"] is not None:
                c["tool_calls"] = json.loads(json.dumps(c["tool_calls"], ensure_ascii=False, default=str))
            cleaned.append(c)
        return cleaned

    def _record_retrieval_step(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        step_index: int,
        title: str,
    ) -> tuple[str, str | None]:
        retrieval_started = time.perf_counter()
        try:
            rag_response = self.rag_chat_service.ask(
                ChatAskRequest(
                    user_id=user_id, team_id=team_id,
                    conversation_id=payload.conversation_id,
                    space_id=space_id,
                    question=self._build_agent_question(task=payload.task, system_prompt=payload.system_prompt),
                    top_k=payload.top_k,
                    selected_document_ids=payload.selected_document_ids,
                    use_document_scope=bool(
                        payload.selected_document_ids or payload.include_library or payload.include_conclusions
                    ),
                    include_memory=payload.include_memory,
                    include_library=payload.include_library,
                    include_conclusions=payload.include_conclusions,
                    model=payload.model,
                    embedding_model=payload.embedding_model,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._record_step(
                run=run, step_index=step_index, step_type="retrieval", title=title,
                status="failed", input_payload={"top_k": payload.top_k},
                output_payload={}, error_message=str(exc),
                latency_ms=self._elapsed_ms(retrieval_started),
            )
            return "", str(exc)

        self._record_step(
            run=run, step_index=step_index, step_type="retrieval", title=title,
            status="completed",
            input_payload={"top_k": payload.top_k, "include_memory": payload.include_memory,
                           "include_library": payload.include_library, "include_conclusions": payload.include_conclusions},
            output_payload={"mode": rag_response.mode, "answer_preview": rag_response.answer[:500],
                            "source_count": len(rag_response.sources),
                            "sources": [item.model_dump() for item in rag_response.sources]},
            latency_ms=self._elapsed_ms(retrieval_started),
        )
        return rag_response.answer, None

    def _execute_tool_calls(
        self,
        *,
        calls: list[AgentToolCall],
        team_id: str,
        user_id: str,
        space_id: str | None,
        task: str,
        dry_run: bool,
        confirm_dangerous_actions: bool,
    ) -> tuple[list[dict[str, object]], list[AgentToolCall], list[str], str]:
        tool_results: list[dict[str, object]] = []
        skipped_calls: list[AgentToolCall] = []
        tool_errors: list[str] = []

        if dry_run:
            return [], [call.model_copy(update={"requires_confirmation": call.dangerous}) for call in calls], [], "skipped"

        for call in calls:
            enriched_call = self._enrich_tool_call(call=call, space_id=space_id, task=task)
            if enriched_call.dangerous and not confirm_dangerous_actions:
                skipped_calls.append(enriched_call.model_copy(update={"requires_confirmation": True}))
                continue
            try:
                result = self.tool_service.execute(
                    team_id=team_id, user_id=user_id,
                    action=enriched_call.tool_name,
                    arguments=enriched_call.arguments,
                )
                tool_results.append({
                    "tool_name": enriched_call.tool_name,
                    "arguments": enriched_call.arguments,
                    "result": result,
                })
            except Exception as exc:  # noqa: BLE001
                tool_errors.append(f"{enriched_call.tool_name}: {exc}")

        if tool_errors:
            return tool_results, skipped_calls, tool_errors, "partial_success" if tool_results else "failed"
        if skipped_calls:
            return tool_results, skipped_calls, tool_errors, "requires_confirmation"
        return tool_results, skipped_calls, tool_errors, "completed"

    # ------------------------------------------------------------------
    # Run lifecycle: create, record, finish, replay
    # ------------------------------------------------------------------

    def _create_queued_run(self, *, payload: AgentRunRequest, team_id: str, user_id: str) -> AgentRun:
        effective_space_id = self.document_service.resolve_space_id(
            team_id=team_id,
            conversation_id=payload.conversation_id,
            space_id=payload.space_id,
            user_id=user_id,
        )
        run = AgentRun(
            run_id=str(uuid4()),
            team_id=team_id,
            user_id=user_id,
            conversation_id=payload.conversation_id,
            space_id=effective_space_id,
            app_id=payload.app_id,
            app_version=payload.app_version,
            trigger_channel=payload.trigger_channel,
            task=payload.task.strip(),
            status="queued",
            final_answer="",
            model=payload.model,
            dry_run=payload.dry_run,
            max_steps=payload.max_steps,
            required_confirmations_json="[]",
            request_payload_json=self._to_json(payload.model_dump()),
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def _create_run(self, *, payload: AgentRunRequest, team_id: str, user_id: str, space_id: str | None) -> AgentRun:
        run = AgentRun(
            run_id=str(uuid4()),
            team_id=team_id,
            user_id=user_id,
            conversation_id=payload.conversation_id,
            space_id=space_id,
            app_id=payload.app_id,
            app_version=payload.app_version,
            trigger_channel=payload.trigger_channel,
            task=payload.task.strip(),
            status="running",
            final_answer="",
            model=payload.model,
            dry_run=payload.dry_run,
            max_steps=payload.max_steps,
            required_confirmations_json="[]",
            request_payload_json=self._to_json(payload.model_dump()),
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def _load_request_payload(self, run: AgentRun) -> AgentRunRequest:
        raw = self._safe_json_dict(run.request_payload_json)
        return AgentRunRequest.model_validate(raw)

    def _replay_existing_run(self, run: AgentRun) -> Iterator[AgentStreamEvent]:
        """Replay steps for an already-completed run."""
        steps = self._load_steps(run.run_id)
        seq = 0
        yield AgentStreamEvent(
            event="run.started", run_id=run.run_id, seq=seq,
            payload={"status": run.status},
        )
        for step in steps:
            seq += 1
            yield AgentStreamEvent(
                event="step.completed", run_id=run.run_id, step_index=step.step_index, seq=seq,
                payload=AgentStepResponse.from_record(step).model_dump(mode="json"),
            )
        seq += 1
        full_response = self.get_run(run_id=run.run_id, team_id=run.team_id, user_id=run.user_id)
        yield AgentStreamEvent(
            event="run.completed", run_id=run.run_id, seq=seq,
            payload=full_response.model_dump(mode="json"),
        )

    def _run_response_dict(self, response: AgentRunResponse | None) -> dict[str, Any]:
        if response is None:
            return {}
        return response.model_dump(mode="json")

    def _record_step(
        self,
        *,
        run: AgentRun,
        step_index: int,
        step_type: str,
        title: str,
        status: str,
        input_payload: dict[str, object],
        output_payload: dict[str, object],
        tool_name: str | None = None,
        error_message: str | None = None,
        latency_ms: int | None = None,
    ) -> AgentStep:
        step = AgentStep(
            step_id=str(uuid4()),
            run_id=run.run_id,
            team_id=run.team_id,
            user_id=run.user_id,
            step_index=step_index,
            step_type=step_type,
            title=title[:128],
            status=status,
            tool_name=tool_name,
            input_json=self._to_json(input_payload),
            output_json=self._to_json(output_payload),
            error_message=error_message,
            latency_ms=latency_ms,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(step)
        self.db.commit()
        self.db.refresh(step)
        return step

    def _finish_run(
        self,
        *,
        run: AgentRun,
        status: str,
        final_answer: str,
        required_confirmations: list[AgentToolCall],
        started: float,
    ) -> None:
        run.status = status
        run.final_answer = final_answer
        run.required_confirmations_json = self._to_json([item.model_dump() for item in required_confirmations])
        run.latency_ms = self._elapsed_ms(started)
        run.completed_at = datetime.now(timezone.utc)
        # Keep loop_state_json for requires_confirmation status (needed by confirm_run)
        if status != "requires_confirmation":
            run.loop_state_json = None
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

    # ------------------------------------------------------------------
    # Keyword planning (fallback — kept intact from original)
    # ------------------------------------------------------------------

    def _plan_tool_calls(
        self, task: str, *, enabled_tools: list[str] | None, space_id: str | None,
    ) -> list[AgentToolCall]:
        normalized = task.strip()
        lowered = normalized.lower()
        calls: list[AgentToolCall] = []

        if self._should_search_knowledge(lowered):
            calls.append(self._build_tool_call(
                tool_name="search_knowledge",
                arguments={"query": normalized, "limit": self._extract_limit(lowered), "space_id": space_id},
            ))
        if self._should_list_recent_documents(lowered):
            calls.append(self._build_tool_call(
                tool_name="list_recent_documents",
                arguments={"limit": self._extract_limit(lowered), "space_id": space_id},
            ))
        if self._should_create_incident(normalized, lowered):
            calls.append(self._build_tool_call(
                tool_name="create_incident",
                arguments={"title": self._extract_incident_title(normalized), "severity": self._extract_severity(normalized)},
            ))
        if self._should_create_memory(lowered):
            calls.append(self._build_tool_call(
                tool_name="create_memory_card",
                arguments={"space_id": space_id, "title": self._extract_title(normalized, fallback="Agent Memory"),
                           "content": normalized, "category": "agent"},
            ))
        if self._should_promote_conclusion(lowered):
            calls.append(self._build_tool_call(
                tool_name="promote_to_conclusion",
                arguments={"space_id": space_id, "title": self._extract_title(normalized, fallback="Agent Conclusion"),
                           "content": normalized, "topic": "agent", "status": "draft"},
            ))
        if self._should_generate_report(lowered):
            calls.append(self._build_tool_call(
                tool_name="generate_incident_report",
                arguments={"title": self._extract_title(normalized, fallback="Incident Report"), "incident_summary": normalized},
            ))
        return self._filter_enabled_tools(calls, enabled_tools=enabled_tools)

    def _build_tool_call(self, *, tool_name: str, arguments: dict[str, object]) -> AgentToolCall:
        definition = self.tool_service.get_tool_definition(tool_name)
        dangerous = bool(definition.dangerous) if definition is not None else False
        clean_args = {key: value for key, value in arguments.items() if value is not None}
        return AgentToolCall(tool_name=tool_name, arguments=clean_args, dangerous=dangerous, requires_confirmation=dangerous)

    def _tool_call_from_workflow_node(self, *, node: dict[str, Any], space_id: str | None, task: str) -> AgentToolCall:
        tool_name = str(node.get("tool_name") or node.get("name") or "").strip()
        if not tool_name:
            raise DomainValidationError("workflow tool_call node requires tool_name.")
        arguments = node.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        call = self._build_tool_call(tool_name=tool_name, arguments=dict(arguments))
        return self._enrich_tool_call(call=call, space_id=space_id, task=task)

    def _enrich_tool_call(self, *, call: AgentToolCall, space_id: str | None, task: str) -> AgentToolCall:
        arguments = dict(call.arguments)
        if space_id and call.tool_name in {"search_knowledge", "list_recent_documents", "create_memory_card", "promote_to_conclusion"}:
            arguments.setdefault("space_id", space_id)
        if call.tool_name == "search_knowledge":
            arguments.setdefault("query", task)
            arguments.setdefault("limit", 5)
        if call.tool_name == "create_memory_card":
            arguments.setdefault("title", self._extract_title(task, fallback="Agent Memory"))
            arguments.setdefault("content", task)
        if call.tool_name == "promote_to_conclusion":
            arguments.setdefault("title", self._extract_title(task, fallback="Agent Conclusion"))
            arguments.setdefault("content", task)
        if call.tool_name == "generate_incident_report":
            arguments.setdefault("incident_summary", task)
        return call.model_copy(update={"arguments": arguments})

    def _filter_enabled_tools(self, calls: list[AgentToolCall], *, enabled_tools: list[str] | None) -> list[AgentToolCall]:
        if enabled_tools is None:
            return calls
        allowed = {item.strip().lower() for item in enabled_tools if str(item).strip()}
        return [call for call in calls if call.tool_name.strip().lower() in allowed]

    # ------------------------------------------------------------------
    # Keyword detectors (unchanged)
    # ------------------------------------------------------------------

    def _should_search_knowledge(self, lowered: str) -> bool:
        keywords = ["search knowledge", "knowledge base", "runbook", "知识库", "处理手册", "手册", "查资料", "检索资料"]
        return any(keyword in lowered for keyword in keywords)

    def _should_list_recent_documents(self, lowered: str) -> bool:
        keywords = ["recent document", "recent documents", "list documents", "latest document",
                    "最近文档", "最近的文档", "最近导入", "查看文档", "列出文档", "查文档"]
        return any(keyword in lowered for keyword in keywords)

    def _should_create_incident(self, task: str, lowered: str) -> bool:
        if "不要创建" in task or "do not create" in lowered:
            return False
        keywords = ["create incident", "open incident", "incident", "创建 incident", "创建事件",
                    "新建事件", "创建故障", "告警", "故障", "事故"]
        return any(keyword in lowered or keyword in task for keyword in keywords)

    def _should_create_memory(self, lowered: str) -> bool:
        keywords = ["create memory", "memory card", "记忆卡", "长期记忆", "沉淀记忆", "保存为记忆"]
        return any(keyword in lowered for keyword in keywords)

    def _should_promote_conclusion(self, lowered: str) -> bool:
        keywords = ["promote to conclusion", "create conclusion", "沉淀结论", "保存结论", "沉淀到知识库", "写入知识库"]
        return any(keyword in lowered for keyword in keywords)

    def _should_generate_report(self, lowered: str) -> bool:
        keywords = ["incident report", "postmortem", "生成报告", "事件报告", "处理报告"]
        return any(keyword in lowered for keyword in keywords)

    def _extract_limit(self, text: str) -> int:
        match = re.search(r"(?:limit|前|最近)\s*(\d{1,2})", text)
        if not match:
            return 5
        return max(1, min(int(match.group(1)), 20))

    def _extract_severity(self, task: str) -> str:
        match = re.search(r"\bP([0-9])\b", task, flags=re.IGNORECASE)
        if match:
            return f"P{match.group(1)}".upper()
        lowered = task.lower()
        if any(token in lowered for token in ["critical", "紧急", "严重", "宕机", "不可用", "cpu 告警", "cpu告警"]):
            return "P1"
        if any(token in lowered for token in ["minor", "低优先级", "轻微"]):
            return "P3"
        return "P2"

    def _extract_incident_title(self, task: str) -> str:
        if "空标题" in task or "empty title" in task.lower():
            return ""
        for marker in ["：", ":", "标题为", "title"]:
            if marker in task:
                candidate = task.split(marker, 1)[1].strip(" ，。:：")
                if candidate:
                    return candidate[:255]
        cleaned = re.sub(r"\s+", " ", task).strip(" ，。:：")
        return cleaned[:255]

    def _extract_title(self, task: str, *, fallback: str) -> str:
        for marker in ["标题为", "title", "：", ":"]:
            if marker in task:
                candidate = task.split(marker, 1)[1].strip(" ，。:：")
                if candidate:
                    return candidate[:128]
        cleaned = re.sub(r"\s+", " ", task).strip(" ，。:：")
        return (cleaned[:80] or fallback)[:128]

    # ------------------------------------------------------------------
    # Status, answer building, identity, access guards
    # ------------------------------------------------------------------

    def _resolve_run_status(self, *, dry_run: bool, skipped_calls: list[AgentToolCall],
                             tool_errors: list[str], tool_results: list[dict[str, object]]) -> str:
        if dry_run:
            return "completed"
        if skipped_calls:
            return "requires_confirmation"
        if tool_errors:
            return "partial_success" if tool_results else "failed"
        return "completed"

    def _build_final_answer(self, *, status: str, task: str, rag_answer: str,
                             tool_results: list[dict[str, object]], skipped_calls: list[AgentToolCall],
                             tool_errors: list[str], dry_run: bool, confirmed: bool = False) -> str:
        lines: list[str] = []
        if dry_run:
            lines.append("Dry run：已生成执行计划，但没有真正调用工具。")
        elif status == "requires_confirmation":
            lines.append("已完成安全步骤；以下高风险动作需要确认后才会执行。")
        elif confirmed:
            lines.append("已根据确认执行待处理工具。")
        elif status == "failed":
            lines.append("Agent 执行失败，未完成任务。")
        elif status == "partial_success":
            lines.append("Agent 已完成部分步骤，但有工具调用失败。")
        else:
            lines.append("Agent 已完成任务。")

        if rag_answer.strip():
            lines.append("")
            lines.append("资料与上下文结论：")
            lines.append(rag_answer.strip())

        if tool_results:
            lines.append("")
            lines.append("工具执行结果：")
            for item in tool_results:
                result = item.get("result")
                if isinstance(result, dict):
                    message = str(result.get("message") or result.get("tool_name") or item.get("tool_name"))
                else:
                    message = str(item.get("tool_name"))
                lines.append(f"- {item.get('tool_name')}: {message}")

        if skipped_calls:
            lines.append("")
            lines.append("待确认动作：")
            for call in skipped_calls:
                lines.append(f"- {call.tool_name}: {call.arguments}")

        if tool_errors:
            lines.append("")
            lines.append("错误：")
            for error in tool_errors:
                lines.append(f"- {error}")

        if not rag_answer.strip() and not tool_results and not skipped_calls and not tool_errors:
            lines.append("")
            lines.append(f"任务：{task}")

        return "\n".join(lines).strip()

    def _build_agent_question(self, *, task: str, system_prompt: str | None) -> str:
        prompt = (system_prompt or "").strip()
        if not prompt:
            return task
        return f"{prompt}\n\n用户任务：{task}"

    def _require_identity(self, team_id: str | None, user_id: str | None) -> tuple[str, str]:
        normalized_team_id = str(team_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        if not normalized_team_id or not normalized_user_id:
            raise DomainValidationError("team_id and user_id are required.")
        return normalized_team_id, normalized_user_id

    # ------------------------------------------------------------------
    # Shared utilities
    # ------------------------------------------------------------------

    def _workflow_nodes(self, workflow_config: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = workflow_config.get("nodes") if isinstance(workflow_config, dict) else None
        if not isinstance(nodes, list):
            return []
        return [dict(node) for node in nodes if isinstance(node, dict)]

    def _record_agent_history(self, *, payload: AgentRunRequest, response: AgentRunResponse) -> None:
        self.chat_history_service.record_message(
            team_id=response.team_id, user_id=response.user_id,
            conversation_id=response.conversation_id, channel="agent",
            request_text=payload.task, response_text=response.answer,
            request_payload=payload.model_dump(),
            response_payload=response.model_dump(mode="json"),
        )

    def _ensure_run_access(self, *, run_id: str, team_id: str, user_id: str) -> AgentRun:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        run = self.db.get(AgentRun, run_id)
        if run is None or run.team_id != team_id or run.user_id != user_id:
            raise EntityNotFoundError(f"Agent run '{run_id}' not found.")
        return run

    def _load_steps(self, run_id: str) -> list[AgentStep]:
        stmt = select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.step_index.asc())
        return list(self.db.scalars(stmt).all())

    def _next_step_index(self, run_id: str) -> int:
        steps = self._load_steps(run_id)
        if not steps:
            return 1
        return max(item.step_index for item in steps) + 1

    def _load_required_confirmations(self, run: AgentRun) -> list[AgentToolCall]:
        raw_items = self._safe_json_list(run.required_confirmations_json)
        calls: list[AgentToolCall] = []
        for item in raw_items:
            if isinstance(item, dict):
                calls.append(AgentToolCall.model_validate(item))
        return calls

    def _extract_tool_calls_from_steps(self, steps: list[AgentStep]) -> list[AgentToolCall]:
        calls: list[AgentToolCall] = []
        for step in steps:
            if step.step_type not in {"plan", "tool_call"}:
                continue
            output = self._safe_json_dict(step.output_json)
            raw_calls = output.get("planned_tool_calls")
            if raw_calls is None:
                input_payload = self._safe_json_dict(step.input_json)
                raw_calls = input_payload.get("calls")

            # Per-tool step format (LLM tool loop): tool_name + arguments at input top-level
            if raw_calls is None and step.tool_name:
                input_payload = self._safe_json_dict(step.input_json)
                tool_name = step.tool_name or input_payload.get("tool_name")
                arguments = input_payload.get("arguments", {})
                if isinstance(tool_name, str) and tool_name.strip():
                    candidate = AgentToolCall(
                        tool_name=tool_name.strip(),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                    if all(existing.tool_name != candidate.tool_name or existing.arguments != candidate.arguments
                           for existing in calls):
                        calls.append(candidate)
                    continue

            if not isinstance(raw_calls, list):
                continue
            for item in raw_calls:
                if isinstance(item, dict):
                    candidate = AgentToolCall.model_validate(item)
                    if all(existing.tool_name != candidate.tool_name or existing.arguments != candidate.arguments
                           for existing in calls):
                        calls.append(candidate)
        return calls

    def _extract_sources_from_steps(self, steps: list[AgentStep]) -> list[ChatSource]:
        all_sources: list[ChatSource] = []
        seen_ids: set[str] = set()
        for step in steps:
            if step.step_type != "retrieval":
                continue
            output = self._safe_json_dict(step.output_json)
            raw_sources = output.get("sources")
            if not isinstance(raw_sources, list):
                continue
            for item in raw_sources:
                if isinstance(item, dict):
                    source = ChatSource.model_validate(item)
                    if source.document_id and source.document_id not in seen_ids:
                        seen_ids.add(source.document_id)
                        all_sources.append(source)
        return all_sources

    def _to_json(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    def _safe_json_dict(self, raw: str) -> dict[str, object]:
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _safe_json_list(self, raw: str) -> list[object]:
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        return decoded if isinstance(decoded, list) else []

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((time.perf_counter() - started) * 1000))
