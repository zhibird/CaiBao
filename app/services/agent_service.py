from __future__ import annotations

import json
import hashlib
import hmac
import re
import time
from datetime import datetime, timezone
from typing import Any, Generator, Iterator
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.agent_run import AgentRun
from app.models.agent_step import AgentStep
from app.models.chat_history import ChatHistory
from app.schemas.agent import (
    AgentRunRequest,
    AgentExecutionReceipt,
    AgentRunResponse,
    AgentRunStartResponse,
    AgentStepResponse,
    AgentStreamEvent,
    AgentToolCall,
)
from app.schemas.chat import ChatAskRequest, ChatSource
from app.core.config import get_settings
from app.events.event_bus import EventBus
from app.events.lifecycle import (
    RetrievalCompleted,
    StepCompleted,
    ToolCalled,
    ToolResulted,
    TurnCommitted,
    TurnStarted,
)
from app.services.chat_history_service import ChatHistoryService
from app.services.cli_agent_service import (
    CLI_AGENT_MODES,
    create_cli_agent_runner,
    resolve_cli_workspace,
)
from app.services.cli_session_store import CliSessionStore
from app.services.conversation_context import build_conversation_context, render_cli_context
from app.services.execution_identity import (
    build_api_execution_identity, build_cli_execution_identity, safe_execution_endpoint,
)
from app.services.execution_ledger import ToolLoopLedger
from app.services.document_service import DocumentService
from app.services.llm_model_service import LLMModelService
from app.services.llm_router import LLMRouter
from app.services.llm_service import LLMCompletionResult, LLMService, LLMStreamChunk
from app.services.memory_markdown_store import MemoryMarkdownStore
from app.services.persona_prompt import PersonaPromptBuilder
from app.services.time_envelope import build_current_message_time_envelope
from app.services.rag_chat_service import RagChatService

from app.services.tool_service import ToolService
from app.services.user_service import UserService

_MAX_TOOL_LOOP_ITERATIONS = 15
_MAX_CONSECUTIVE_DUPLICATE_CALLS = 2
_CANCELLED_ANSWER = "任务已停止。已经开始的工具可能仍会完成，后续操作不会继续。"


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
        event_bus: EventBus | None = None,
        memory_store: MemoryMarkdownStore | None = None,
        plugin_manager=None,  # optional PluginManager
        run_runtime=None,
    ) -> None:
        self.db = db
        self.user_service = user_service
        self.document_service = document_service
        self.rag_chat_service = rag_chat_service
        self.tool_service = tool_service
        self.chat_history_service = chat_history_service
        self.llm_service = llm_service
        self.llm_model_service = llm_model_service
        self.event_bus = event_bus
        self.memory_store = memory_store
        self.persona_prompt_builder = PersonaPromptBuilder(memory_store=memory_store)
        self.plugin_manager = plugin_manager
        self.run_runtime = run_runtime
        self._frozen_cli_settings = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, payload: AgentRunRequest) -> AgentRunStartResponse:
        """Create a queued run, returning run_id and stream_url."""
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        self._validate_run_mode(payload.run_mode)

        run = self._find_existing_request(payload, team_id=team_id, user_id=user_id)
        if run is None:
            try:
                run = self._create_queued_run(payload=payload, team_id=team_id, user_id=user_id)
            except IntegrityError:
                # The unique request index arbitrates concurrent lost-response retries.
                self.db.rollback()
                run = self._find_existing_request(payload, team_id=team_id, user_id=user_id)
                if run is None:
                    raise
        return AgentRunStartResponse(
            run_id=run.run_id,
            stream_url=f"/agent/runs/{run.run_id}/stream",
            status=run.status,
        )

    def _find_existing_request(self, payload: AgentRunRequest, *, team_id: str, user_id: str) -> AgentRun | None:
        if payload.client_request_id is None:
            return None
        run = self.db.scalar(select(AgentRun).where(
            AgentRun.team_id == team_id, AgentRun.user_id == user_id,
            AgentRun.client_request_id == payload.client_request_id,
        ))
        if run is not None:
            original = self._load_request_payload(run).model_dump()
            requested = payload.model_dump()
            original["task"], requested["task"] = original["task"].strip(), requested["task"].strip()
            if original != requested:
                raise DomainValidationError("This client_request_id already belongs to a different task or configuration.")
        return run

    @staticmethod
    def _approval_token(run: AgentRun) -> str | None:
        if run.status != "requires_confirmation":
            return None
        # Include the saved model turn as well as arguments: two identical writes
        # proposed in different turns are still two separate approvals.
        identity = [run.run_id, run.required_confirmations_json, run.loop_state_json]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_run_mode(run_mode: str) -> None:
        """未知 run_mode 直接 400，而不是静默降级到内置 agent_auto 循环。"""
        normalized = run_mode.strip().lower()
        allowed = {"agent_auto", "workflow"} | CLI_AGENT_MODES
        if normalized not in allowed:
            raise DomainValidationError(
                f"Unknown run_mode '{run_mode}'. Allowed: {', '.join(sorted(allowed))}."
            )

    def run(self, payload: AgentRunRequest) -> AgentRunResponse:
        """Synchronous run (backward-compatible)."""
        if payload.client_request_id is not None:
            raise DomainValidationError("client_request_id requires the queued /agent/runs interface.")
        if getattr(self, "run_runtime", None) is not None:
            admitted = self.start(payload)
            record = self.db.get(AgentRun, admitted.run_id)
            # Preserve the synchronous model protocol while moving ownership
            # to the same worker/lease as streaming entry points. This marker
            # is written internally, never accepted from request extra fields.
            saved = self._safe_json_dict(record.request_payload_json)
            saved["_completion_mode"] = "synchronous"
            record.request_payload_json = self._to_json(saved)
            self.db.commit()
            try:
                self.run_runtime.ensure_started(admitted.run_id)
            except DomainValidationError:
                # A synchronous caller has not received a run ID yet. Leave no
                # hidden queued job which could execute after the rejection.
                self.cancel_run(run_id=admitted.run_id, team_id=payload.team_id, user_id=payload.user_id)
                raise
            for _event in self.run_runtime.iter_events(admitted.run_id):
                pass
            self.db.expire_all()
            return self.get_run(run_id=admitted.run_id, team_id=payload.team_id, user_id=payload.user_id)
        team_id, user_id = self._require_identity(payload.team_id, payload.user_id)
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        self._validate_run_mode(payload.run_mode)
        effective_space_id = self.document_service.resolve_space_id(
            team_id=team_id,
            conversation_id=payload.conversation_id,
            space_id=payload.space_id,
            user_id=user_id,
        )
        started = time.perf_counter()
        run = self._create_run(payload=payload, team_id=team_id, user_id=user_id, space_id=effective_space_id)
        routes = self._prepare_execution(run, payload)

        if self.event_bus:
            self.event_bus.emit(TurnStarted(
                run_id=run.run_id, team_id=team_id, user_id=user_id,
                conversation_id=payload.conversation_id, space_id=effective_space_id,
                task=payload.task, channel=payload.trigger_channel,
            ))

        self._run_plugin_phase("BeforeTurn", run.run_id, team_id, user_id, effective_space_id,
                               run=run, payload=payload, routes=routes)

        run_mode = payload.run_mode.strip().lower()
        if run_mode in CLI_AGENT_MODES:
            response = self._run_cli_agent(
                run=run, payload=payload, team_id=team_id, user_id=user_id,
                space_id=effective_space_id, started=started,
            )
        elif run_mode == "workflow" and self._workflow_nodes(payload.workflow_config):
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

        # Claim once: opening a second stream must not start the same work twice,
        # or revive a run cancelled after the initial read.
        claimed = self.db.execute(
            update(AgentRun).where(AgentRun.run_id == run_id, AgentRun.status == "queued")
            .values(status="running").execution_options(synchronize_session=False)
        ).rowcount
        self.db.commit()
        self.db.refresh(run)
        if not claimed:
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
        routes = self._prepare_execution(run, payload)

        seq = 0
        yield AgentStreamEvent(
            event="run.started", run_id=run.run_id, seq=seq,
            payload={"status": "running"},
        )

        if self.event_bus:
            self.event_bus.emit(TurnStarted(
                run_id=run.run_id, team_id=team_id, user_id=user_id,
                conversation_id=run.conversation_id, space_id=effective_space_id,
                task=payload.task, channel=run.trigger_channel,
            ))

        self._run_plugin_phase("BeforeTurn", run.run_id, team_id, user_id, effective_space_id,
                               run=run, payload=payload, routes=routes)

        started = time.perf_counter()
        try:
            run_mode = payload.run_mode.strip().lower()
            if run_mode in CLI_AGENT_MODES:
                gen = self._run_cli_agent_stream(
                    run=run, payload=payload, team_id=team_id, user_id=user_id,
                    space_id=effective_space_id, started=started,
                )
                seq = None
                try:
                    for event in gen:
                        seq = event.seq
                        yield event
                finally:
                    gen.close()
                response = self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)
                self._record_agent_history(payload=payload, response=response)
            elif run_mode == "workflow" and self._workflow_nodes(payload.workflow_config):
                response = self._run_workflow(
                    run=run, payload=payload, team_id=team_id, user_id=user_id,
                    space_id=effective_space_id, started=started, stream_sink=True,
                )
            elif self._safe_json_dict(run.request_payload_json).get("_completion_mode") == "synchronous":
                response = self._run_agent_auto_tool_loop(
                    run=run, payload=payload, team_id=team_id, user_id=user_id,
                    space_id=effective_space_id, started=started, routes=routes,
                )
                self._record_agent_history(payload=payload, response=response)
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
                self._record_agent_history(payload=payload, response=response)
        except Exception as exc:
            if self._is_run_cancelled(run):
                response = self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)
                yield AgentStreamEvent(
                    event="run.interrupted" if response.status == "interrupted" else "run.cancelled", run_id=run.run_id, seq=(seq or 0) + 1,
                    payload=self._run_response_dict(response),
                )
                return
            self._finish_run(run=run, status="failed", final_answer=f"Agent execution failed: {exc}",
                             required_confirmations=[], started=started)
            response = self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)
            self._record_agent_history(payload=payload, response=response)
            seq = (seq or 0) + 1
            yield AgentStreamEvent(
                event="run.failed", run_id=run.run_id, seq=seq,
                payload={**response.model_dump(mode="json"), "error": str(exc)},
            )
            return

        seq = (seq or 0) + 1
        yield AgentStreamEvent(
            event="run.interrupted" if response.status == "interrupted" else "run.cancelled" if response.status == "cancelled" else "run.completed", run_id=run.run_id, seq=seq,
            payload=self._run_response_dict(response),
        )

    def cancel_run(self, *, run_id: str, team_id: str, user_id: str) -> AgentRunResponse:
        """Request cooperative stop; an in-flight network/tool call may finish."""
        run = self._ensure_run_access(run_id=run_id, team_id=team_id, user_id=user_id)
        if run.status not in {"queued", "running", "requires_confirmation"}:
            return self.get_run(run_id=run_id, team_id=team_id, user_id=user_id)
        payload = self._load_request_payload(run)
        supported = payload.run_mode.strip().lower() == "agent_auto"
        allowed_statuses = {"queued", "requires_confirmation"} | ({"running"} if supported else set())
        if run.status not in allowed_statuses:
            raise DomainValidationError("Running CLI and workflow tasks do not support cooperative cancellation.")
        changed = self.db.execute(
            update(AgentRun).where(AgentRun.run_id == run_id, AgentRun.status.in_(allowed_statuses))
            .values(status="cancelled", final_answer=_CANCELLED_ANSWER,
                    required_confirmations_json="[]", loop_state_json=None,
                    completed_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        ).rowcount
        self.db.commit()
        self.db.refresh(run)
        if not changed and run.status == "running" and not supported:
            raise DomainValidationError("Running CLI and workflow tasks do not support cooperative cancellation.")
        return self.get_run(run_id=run_id, team_id=team_id, user_id=user_id)

    def _is_run_cancelled(self, run: AgentRun) -> bool:
        if not isinstance(run, AgentRun):
            return getattr(run, "status", None) in {"cancelled", "interrupted"}
        # Read a scalar column so an ORM identity cached by the executing request
        # cannot hide a stop committed by a separate HTTP request/session.
        with self.db.no_autoflush:
            return self.db.scalar(select(AgentRun.status).where(AgentRun.run_id == run.run_id)) in {"cancelled", "interrupted"}

    def confirm_run(self, *, run_id: str, team_id: str, user_id: str,
                    approval_token: str | None = None) -> AgentRunResponse:
        run = self._ensure_run_access(run_id=run_id, team_id=team_id, user_id=user_id)
        if run.status != "requires_confirmation":
            raise DomainValidationError("Only runs waiting for confirmation can be confirmed.")

        pending_calls = self._load_required_confirmations(run)
        if not pending_calls:
            raise DomainValidationError("This run has no pending tool calls.")

        expected_token = self._approval_token(run)
        requires_token = run.trigger_channel.strip().lower() == "companion"
        if (requires_token or approval_token is not None) and (
            not approval_token or not hmac.compare_digest(
                approval_token.encode("utf-8"), (expected_token or "").encode("utf-8")
            )
        ):
            raise DomainValidationError("This approval has changed. Review the current operation before confirming.")

        loop_state = self._load_loop_state(run)
        real_routes = None
        ledger = None
        if loop_state is None and self._requires_tool_loop_checkpoint(run):
            raise DomainValidationError(
                "本次待确认任务的执行检查点已丢失，暂不执行。请取消后新建任务。"
            )
        if loop_state is not None:
            # Reconstruct/check before claiming consent or invoking any handler.
            # An old or corrupt checkpoint must never grant a new budget.
            ledger = self._restore_execution_ledger(run, loop_state, pending_calls[0])
            payload = self._load_request_payload(run)
            real_routes = self._resolve_routes(team_id=team_id, user_id=user_id, payload=payload)
            frozen = self._safe_json_dict(run.execution_identity_json) if run.execution_identity_json else {}
            if frozen and frozen.get("binding") != self._api_execution_identity(real_routes, payload).get("binding"):
                raise DomainValidationError(
                    "本次任务的模型或连接配置已变化。请恢复原配置，或取消后新建任务；不会用另一个模型继续这次授权。"
                )
        claimed = self.db.execute(
            update(AgentRun).where(
                AgentRun.run_id == run_id, AgentRun.status == "requires_confirmation",
                AgentRun.required_confirmations_json == run.required_confirmations_json,
                AgentRun.loop_state_json == run.loop_state_json,
            )
            .values(status="running").execution_options(synchronize_session=False)
        ).rowcount
        self.db.commit()
        self.db.refresh(run)
        if not claimed:
            if run.status == "cancelled":
                return self.get_run(run_id=run_id, team_id=team_id, user_id=user_id)
            raise DomainValidationError("This confirmation is already being processed.")
        if loop_state is None:
            # Keyword-fallback path: no LLM loop to resume, just execute and finish.
            response = self._confirm_fallback(run=run, pending_calls=pending_calls,
                                             team_id=team_id, user_id=user_id)
            self._record_agent_history(payload=self._load_request_payload(run), response=response)
            return response

        if self.event_bus:
            self.event_bus.emit(TurnStarted(
                run_id=run.run_id, team_id=team_id, user_id=user_id,
                conversation_id=run.conversation_id, space_id=run.space_id,
                task=run.task, channel=run.trigger_channel,
            ))

        started = time.perf_counter()

        # The live credentials were resolved and checked before claiming this
        # exact approval. Keep that one route object for the continuation.
        payload = self._load_request_payload(run)
        planner = self._routes_to_planner_dict(real_routes)

        # Execute the pending tool call
        next_index = self._next_step_index(run.run_id)
        call = pending_calls[0]
        if self.event_bus:
            self.event_bus.emit(ToolCalled(
                run_id=run.run_id, step_index=next_index,
                tool_name=call.tool_name, arguments=call.arguments,
            ))
        tool_started = time.perf_counter()
        if self._is_run_cancelled(run):
            return self.get_run(run_id=run_id, team_id=team_id, user_id=user_id)
        try:
            result = self.tool_service.execute(
                team_id=team_id, user_id=user_id,
                action=call.tool_name, arguments=call.arguments,
                run_id=run.run_id, confirmed=True,
            )
            tool_result_entry, tool_status = self._observed_tool_result(call.tool_name, call.arguments, result)
        except Exception as exc:  # noqa: BLE001
            tool_result_entry = {"tool_name": call.tool_name, "arguments": call.arguments, "error": str(exc)}
            tool_status = "failed"
        if self.event_bus:
            self.event_bus.emit(ToolResulted(
                run_id=run.run_id, step_index=next_index,
                tool_name=call.tool_name, status=tool_status,
                error=str(tool_result_entry.get("error")) if "error" in tool_result_entry else None,
            ))

        self._record_step(
            run=run, step_index=next_index, step_type="tool_call",
            title=f"Execute confirmed tool: {call.tool_name}", status=tool_status,
            tool_name=call.tool_name,
            input_payload={"calls": [call.model_dump()], "confirmed": True},
            output_payload={"executed": [tool_result_entry]},
            error_message=str(tool_result_entry.get("error")) if "error" in tool_result_entry else None,
            latency_ms=self._elapsed_ms(tool_started),
        )

        ledger.resolve(tool_status)
        ledger.stop_reason = "in_progress"
        self._persist_execution_receipt(run, ledger)

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
        final_answer, final_status, requires_confirmation = self._tool_loop(
            run=run, messages=messages, tools=tools,
            team_id=team_id, user_id=user_id, space_id=run.space_id,
            base_url=planner["base_url"],
            api_key=planner["api_key"],
            model_name=planner["model_name"],
            step_index=step_index, started=started,
            enable_tool_execution=True,
            # Companion consent covers the displayed call, not future writes
            # proposed after the model sees its result.
            confirm_dangerous_actions=payload.trigger_channel.strip().lower() != "companion",
            dry_run=False,
            enabled_tools=payload.enabled_tools,
            max_tool_calls=payload.max_steps,
            ledger=ledger,
        )

        self._record_step(
            run=run, step_index=step_index + 50, step_type="final",
            title="Summarize confirmed result", status=final_status,
            input_payload={"run_id": run_id},
            output_payload={"answer": final_answer, "status": final_status},
        )
        pending = self._load_pending_tool_call(run) if requires_confirmation else None
        self._finish_run(
            run=run, status=final_status, final_answer=final_answer,
            required_confirmations=[pending] if pending else [], started=started,
        )
        response = self.get_run(run_id=run_id, team_id=team_id, user_id=user_id)
        self._record_agent_history(payload=payload, response=response)
        return response

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
            if self._is_run_cancelled(run):
                break
            if self.event_bus:
                self.event_bus.emit(ToolCalled(
                    run_id=run.run_id, step_index=next_index,
                    tool_name=call.tool_name, arguments=call.arguments,
                ))
            try:
                result = self.tool_service.execute(
                    team_id=team_id, user_id=user_id,
                    action=call.tool_name, arguments=call.arguments,
                    run_id=run.run_id, confirmed=True,
                )
                tool_results.append({"tool_name": call.tool_name, "arguments": call.arguments, "result": result})
                if self.event_bus:
                    self.event_bus.emit(ToolResulted(
                        run_id=run.run_id, step_index=next_index,
                        tool_name=call.tool_name, status="completed",
                    ))
            except Exception as exc:  # noqa: BLE001
                tool_errors.append(f"{call.tool_name}: {exc}")
                if self.event_bus:
                    self.event_bus.emit(ToolResulted(
                        run_id=run.run_id, step_index=next_index,
                        tool_name=call.tool_name, status="failed",
                        error=str(exc),
                    ))

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
            client_request_id=run.client_request_id, approval_token=self._approval_token(run),
            execution_identity=self._safe_json_dict(run.execution_identity_json) if run.execution_identity_json else None,
            context_snapshot=self._safe_json_dict(run.context_snapshot_json) if run.context_snapshot_json else None,
            execution_receipt=self._load_execution_receipt(run),
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

    def list_runs(self, *, team_id: str, user_id: str, conversation_id: str | None = None,
                  trigger_channel: str | None = None, status: str | None = None,
                  limit: int = 20) -> list[AgentRunResponse]:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        if conversation_id is not None:
            # Validate ownership even for an empty conversation; do not reveal
            # another user's conversation via the presence or absence of runs.
            self.document_service.resolve_space_id(
                team_id=team_id, user_id=user_id, conversation_id=conversation_id, space_id=None,
            )
        query = select(AgentRun).where(AgentRun.team_id == team_id, AgentRun.user_id == user_id)
        if conversation_id is not None:
            query = query.where(AgentRun.conversation_id == conversation_id)
        if trigger_channel is not None:
            query = query.where(AgentRun.trigger_channel == trigger_channel)
        if status is not None:
            query = query.where(AgentRun.status == status)
        runs = self.db.scalars(query.order_by(AgentRun.created_at.desc(), AgentRun.run_id.desc()).limit(limit))
        return [self.get_run(run_id=item.run_id, team_id=team_id, user_id=user_id) for item in runs]

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
            if self._should_fallback_to_keyword(str(exc)) and self._can_fallback_tool_loop(run):
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
        if self._is_run_cancelled(run):
            return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)
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

        if self.event_bus:
            self.event_bus.emit(RetrievalCompleted(
                run_id=run.run_id, query=payload.task, hit_count=1 if rag_answer else 0,
                injected_count=1 if rag_answer else 0,
                error=retrieval_failed,
            ))

        # Build messages
        messages = self._build_agent_messages(
            task=payload.task, system_prompt=payload.system_prompt,
            rag_answer=rag_answer,
            team_id=team_id, user_id=user_id, space_id=space_id,
            conversation_id=payload.conversation_id,
            include_memory=payload.include_memory,
            channel=payload.trigger_channel,
            run=run,
        )
        tools = self._build_tools(payload=payload, run=run)

        self._run_plugin_phase("PromptRender", run.run_id, team_id, user_id, space_id,
                               run=run, messages=messages)
        self._run_plugin_phase("BeforeReasoning", run.run_id, team_id, user_id, space_id,
                               run=run, messages=messages, tools=tools)

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
            max_tool_calls=payload.max_steps,
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
        self._run_plugin_phase("AfterReasoning", run.run_id, team_id, user_id, space_id,
                               run=run, final_answer=final_answer)
        self._finish_run(run=run, status=final_status, final_answer=final_answer,
                         required_confirmations=[], started=started)
        self._run_plugin_phase("AfterTurn", run.run_id, team_id, user_id, space_id, run=run)
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
        if self._is_run_cancelled(run):
            return
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
        if self._is_run_cancelled(run):
            return
        rag_answer, retrieval_failed = self._record_retrieval_step(
            run=run, payload=payload, team_id=team_id, user_id=user_id,
            space_id=space_id, step_index=2, title="Retrieve workspace context",
        )
        if self.event_bus:
            self.event_bus.emit(RetrievalCompleted(
                run_id=run.run_id, query=payload.task, hit_count=1 if rag_answer else 0,
                injected_count=1 if rag_answer else 0,
                error=retrieval_failed,
            ))
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
            team_id=team_id, user_id=user_id, space_id=space_id,
            conversation_id=payload.conversation_id,
            include_memory=payload.include_memory,
            channel=payload.trigger_channel,
            run=run,
        )
        tools = self._build_tools(payload=payload, run=run)

        self._run_plugin_phase("PromptRender", run.run_id, team_id, user_id, space_id,
                               run=run, messages=messages)
        self._run_plugin_phase("BeforeReasoning", run.run_id, team_id, user_id, space_id,
                               run=run, messages=messages, tools=tools)

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
            max_tool_calls=payload.max_steps,
        )

    # ------------------------------------------------------------------
    # Tool loop (shared between sync and streaming)
    # ------------------------------------------------------------------

    def _tool_loop(
        self, *, run: AgentRun, messages: list[dict[str, object]], tools: list[dict[str, object]],
        team_id: str, user_id: str, space_id: str | None, base_url: str, api_key: str,
        model_name: str, step_index: int, started: float, enable_tool_execution: bool,
        confirm_dangerous_actions: bool, dry_run: bool, enabled_tools: list[str] | None = None,
        max_tool_calls: int = 5, ledger: ToolLoopLedger | None = None,
    ) -> tuple[str, str, bool]:
        engine = self._iterate_tool_loop(
            run=run, messages=messages, tools=tools, team_id=team_id, user_id=user_id,
            space_id=space_id, base_url=base_url, api_key=api_key, model_name=model_name,
            step_index=step_index, start_seq=0, streaming=False,
            confirm_dangerous_actions=confirm_dangerous_actions, dry_run=dry_run or not enable_tool_execution,
            enabled_tools=enabled_tools, max_tool_calls=max_tool_calls, ledger=ledger,
        )
        while True:
            try:
                next(engine)
            except StopIteration as done:
                return done.value

    def _tool_loop_stream(
        self, *, run: AgentRun, messages: list[dict[str, object]], tools: list[dict[str, object]],
        team_id: str, user_id: str, space_id: str | None, base_url: str, api_key: str,
        model_name: str, started: float, start_seq: int, enable_tool_execution: bool,
        confirm_dangerous_actions: bool, dry_run: bool, enabled_tools: list[str] | None = None,
        max_tool_calls: int = 5,
    ) -> Iterator[AgentStreamEvent]:
        engine = self._iterate_tool_loop(
            run=run, messages=messages, tools=tools, team_id=team_id, user_id=user_id,
            space_id=space_id, base_url=base_url, api_key=api_key, model_name=model_name,
            step_index=3, start_seq=start_seq, streaming=True,
            confirm_dangerous_actions=confirm_dangerous_actions, dry_run=dry_run or not enable_tool_execution,
            enabled_tools=enabled_tools, max_tool_calls=max_tool_calls,
        )
        try:
            while True:
                try:
                    yield next(engine)
                except StopIteration as done:
                    final_answer, status, waiting = done.value
                    break
        except DomainValidationError as exc:
            if self._should_fallback_to_keyword(str(exc)) and self._can_fallback_tool_loop(run):
                text = self._run_agent_auto_fallback_sync(
                    run=run, team_id=team_id, user_id=user_id, space_id=space_id, started=started,
                )
                if not self._is_run_cancelled(run):
                    yield AgentStreamEvent(event="llm.delta", run_id=run.run_id, payload={"text": text})
                return
            raise
        finally:
            engine.close()
        if self._is_run_cancelled(run):
            return
        pending = self._load_pending_tool_call(run) if waiting else None
        self._record_step(
            run=run, step_index=self._next_step_index(run.run_id), step_type="final",
            title="Summarize result", status=status, input_payload={"run_id": run.run_id},
            output_payload={"answer": final_answer, "status": status},
        )
        if not waiting:
            self._run_plugin_phase("AfterReasoning", run.run_id, team_id, user_id, space_id,
                                   run=run, final_answer=final_answer)
        self._finish_run(run=run, status=status, final_answer=final_answer,
                         required_confirmations=[pending] if pending else [], started=started)
        if waiting and pending:
            yield AgentStreamEvent(event="confirmation.required", run_id=run.run_id,
                                   payload={**pending.model_dump(), "approval_token": self._approval_token(run)})
        else:
            self._run_plugin_phase("AfterTurn", run.run_id, team_id, user_id, space_id, run=run)

    def _iterate_tool_loop(
        self, *, run: AgentRun, messages: list[dict[str, object]], tools: list[dict[str, object]],
        team_id: str, user_id: str, space_id: str | None, base_url: str, api_key: str,
        model_name: str, step_index: int, start_seq: int, streaming: bool,
        confirm_dangerous_actions: bool, dry_run: bool, enabled_tools: list[str] | None = None,
        max_tool_calls: int = 5, ledger: ToolLoopLedger | None = None,
    ) -> Generator[AgentStreamEvent, None, tuple[str, str, bool]]:
        """One accounting/execution path for sync, stream and approval resume.

        A reservation precedes any handler or approval. Only observed outcomes
        resolve it; a stopped owner cannot manufacture success for an in-flight
        action. Receipt counts never include private arguments or model thoughts.
        """
        ledger = ledger or ToolLoopLedger(budget=max(1, min(int(max_tool_calls or 5), 10)))
        ledger.stop_reason = "in_progress"
        allowed = ({str(name).strip().lower() for name in enabled_tools if str(name).strip()}
                   if enabled_tools is not None else None)
        text_parts = []
        seq, current_step = start_seq, step_index
        stop = None
        for _ in range(_MAX_TOOL_LOOP_ITERATIONS):
            if self._is_run_cancelled(run):
                return _CANCELLED_ANSWER, "cancelled", False
            no_tools = ledger.used >= ledger.budget or stop is not None
            if no_tools:
                ledger.stop_reason = stop or "tool_budget"
                prompt = (self._tool_budget_prompt(ledger.budget) if stop is None else
                          "Tool execution stopped by the repeated-call guard. Do not call more tools. "
                          "Summarize the observed results, what is unfinished, and a useful next step.")
                messages.append({"role": "user", "content": prompt})
            if streaming:
                names, args, indices, reasoning = {}, {}, {}, []
                for chunk in self.llm_service.stream_chat(
                    messages=messages, model=model_name, base_url=base_url, api_key=api_key,
                    tools=None if no_tools else (tools or None),
                ):
                    if self._is_run_cancelled(run):
                        return _CANCELLED_ANSWER, "cancelled", False
                    if chunk.delta_text:
                        text_parts.append(chunk.delta_text); seq += 1
                        yield AgentStreamEvent(event="llm.delta", run_id=run.run_id,
                                               step_index=current_step, seq=seq,
                                               payload={"text": chunk.delta_text})
                    if chunk.reasoning_delta:
                        reasoning.append(chunk.reasoning_delta)
                    for delta in chunk.tool_call_deltas:
                        index = str(delta.get("index", ""))
                        if index not in indices:
                            indices[index] = len(indices)
                        fn = delta.get("function", {})
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                names[index] = str(fn["name"])
                            if fn.get("arguments"):
                                args[index] = args.get(index, "") + str(fn["arguments"])
                calls = self.llm_service.finalize_tool_calls_from_stream(
                    accumulated_names=names, accumulated_args=args, ordered_indices=indices,
                    reasoning_content="".join(reasoning) or None,
                )
            else:
                result = self.llm_service.complete_chat(
                    messages=messages, model=model_name, base_url=base_url, api_key=api_key,
                    tools=None if no_tools else (tools or None),
                )
                if self._is_run_cancelled(run):
                    return _CANCELLED_ANSWER, "cancelled", False
                if result.assistant_text:
                    text_parts.append(result.assistant_text)
                calls = result.tool_calls
            if no_tools:
                # Even a provider which ignores tools=None cannot execute more.
                ledger.record_omitted(len(calls))
                break
            if not calls:
                ledger.stop_reason = "model_finished"
                break
            for position, tc in enumerate(calls):
                if self._is_run_cancelled(run):
                    return _CANCELLED_ANSWER, "cancelled", False
                if ledger.used >= ledger.budget:
                    ledger.record_omitted(len(calls) - position)
                    break
                tool_name, raw_args, parse_error = self._parse_tool_proposal(tc)
                seq += 1
                yield AgentStreamEvent(event="tool.proposed", run_id=run.run_id,
                                       step_index=current_step, seq=seq,
                                       payload={"tool_name": tool_name, "arguments": raw_args})
                if self._is_run_cancelled(run):
                    return _CANCELLED_ANSWER, "cancelled", False
                signature = self._tool_call_signature(tool_name, raw_args)
                if self._count_consecutive(ledger.history, signature) >= _MAX_CONSECUTIVE_DUPLICATE_CALLS:
                    ledger.record_omitted(len(calls) - position)
                    stop = "duplicate_guard"
                    break
                definition = self.tool_service.get_tool_definition(tool_name) if not parse_error else None
                normalized = raw_args
                error = parse_error
                if not error:
                    if definition is None or not getattr(definition, "enabled", True):
                        error = f"Unknown or disabled tool: {tool_name}"
                    elif allowed is not None and tool_name.lower() not in allowed:
                        error = f"Tool '{tool_name}' is not enabled for this run."
                    else:
                        try:
                            normalized = definition.normalize_arguments(raw_args)
                            normalized = self._enrich_tool_call(
                                call=AgentToolCall(tool_name=tool_name, arguments=normalized),
                                space_id=space_id, task=run.task,
                            ).arguments
                            errors = definition.validate_arguments(normalized)
                            if errors:
                                error = "Argument validation failed: " + "; ".join(errors)
                        except (ValueError, TypeError) as exc:
                            error = f"Argument validation failed: {exc}"
                # Signatures describe normalized arguments for valid proposals.
                if not error:
                    signature = self._tool_call_signature(tool_name, normalized)
                    if self._count_consecutive(ledger.history, signature) >= _MAX_CONSECUTIVE_DUPLICATE_CALLS:
                        ledger.record_omitted(len(calls) - position); stop = "duplicate_guard"; break
                ledger.reserve(signature)
                current_step += 1
                if error:
                    ledger.resolve("rejected")
                    tool_result = {"tool_name": tool_name, "error": error, "rejected": True}
                    self._persist_execution_receipt(run, ledger)
                    self._record_step(
                        run=run, step_index=current_step, step_type="tool_call",
                        title=f"Reject: {tool_name}", status="failed", tool_name=tool_name,
                        input_payload={"calls": [{"tool_name": tool_name, "arguments": raw_args}]},
                        output_payload={"executed": [], "errors": [error], "rejected": True},
                        error_message=error,
                    )
                    self._append_tool_result(messages, tc, tool_result)
                    seq += 1
                    yield AgentStreamEvent(event="tool.rejected", run_id=run.run_id,
                                           step_index=current_step, seq=seq,
                                           payload={"tool_name": tool_name, "result": tool_result})
                    continue
                self._persist_execution_receipt(run, ledger)
                if definition.dangerous and not dry_run and not confirm_dangerous_actions:
                    # Only the reviewed call is retained for approval. Other
                    # proposals in that model batch are never silently executed.
                    ledger.record_omitted(len(calls) - position - 1)
                    call = AgentToolCall(tool_name=tool_name, arguments=normalized,
                                         dangerous=True, requires_confirmation=True)
                    self._save_pending_tool_call(run, call)
                    messages.append(self._assistant_tool_call_message(tc))
                    ledger.stop_reason = "waiting_confirmation"
                    self._save_loop_state(run, messages=messages,
                                          pending_tool_call_id=str(tc.get("id", "unknown")), ledger=ledger)
                    self._persist_execution_receipt(run, ledger)
                    return self._loop_text(text_parts, streaming), "requires_confirmation", True
                if self.event_bus:
                    self.event_bus.emit(ToolCalled(run_id=run.run_id, step_index=current_step,
                                                  tool_name=tool_name, arguments=normalized))
                seq += 1
                yield AgentStreamEvent(event="tool.started", run_id=run.run_id,
                                       step_index=current_step, seq=seq, payload={"tool_name": tool_name})
                if self._is_run_cancelled(run):
                    return _CANCELLED_ANSWER, "cancelled", False
                tool_started = time.perf_counter()
                if dry_run:
                    tool_result = {"tool_name": tool_name, "arguments": normalized, "result": {"dry_run": True}}
                    outcome = "skipped"
                else:
                    try:
                        value = self.tool_service.execute(
                            team_id=team_id, user_id=user_id, action=tool_name, arguments=normalized,
                            run_id=run.run_id, dry_run=False,
                            confirmed=confirm_dangerous_actions or not definition.dangerous,
                        )
                        tool_result, outcome = self._observed_tool_result(tool_name, normalized, value)
                    except Exception as exc:  # noqa: BLE001
                        tool_result = {"tool_name": tool_name, "arguments": normalized, "error": str(exc)}
                        outcome = "failed"
                ledger.resolve(outcome)
                self._persist_execution_receipt(run, ledger)
                if self.event_bus:
                    self.event_bus.emit(ToolResulted(run_id=run.run_id, step_index=current_step,
                                                    tool_name=tool_name, status=outcome,
                                                    error=str(tool_result.get("error")) if "error" in tool_result else None))
                self._record_step(
                    run=run, step_index=current_step, step_type="tool_call", title=f"Execute: {tool_name}",
                    status=outcome, tool_name=tool_name,
                    input_payload={"calls": [{"tool_name": tool_name, "arguments": normalized}]},
                    output_payload={"executed": [tool_result], "errors": [str(tool_result["error"])] if "error" in tool_result else []},
                    error_message=str(tool_result.get("error")) if "error" in tool_result else None,
                    latency_ms=self._elapsed_ms(tool_started),
                )
                self._append_tool_result(messages, tc, tool_result)
                if self._is_run_cancelled(run):
                    return _CANCELLED_ANSWER, "cancelled", False
                seq += 1
                yield AgentStreamEvent(event="tool.result", run_id=run.run_id,
                                       step_index=current_step, seq=seq,
                                       payload={"tool_name": tool_name, "result": tool_result})
        else:
            ledger.stop_reason = "iteration_limit"
        self._persist_execution_receipt(run, ledger)
        text = self._loop_text(text_parts, streaming)
        if not text:
            text = ("本轮工具处理已结束，请查看执行回执。" if ledger.final_status() == "completed"
                    else "本轮未确认全部完成，请查看执行回执中的失败或停止原因。")
        return text, ledger.final_status(), False

    @staticmethod
    def _loop_text(parts: list[str], streaming: bool) -> str:
        return ("" if streaming else "\n").join(parts).strip()

    @staticmethod
    def _tool_value_failed(value: object) -> bool:
        # MCP's explicit error flag is a failed tool result even if transport
        # succeeded. Do not infer failure from arbitrary business status text.
        return isinstance(value, dict) and (value.get("is_error") is True or value.get("isError") is True)

    @classmethod
    def _observed_tool_result(cls, name: str, arguments: dict[str, object], value: object) -> tuple[dict[str, object], str]:
        entry = {"tool_name": name, "arguments": arguments, "result": value}
        if cls._tool_value_failed(value):
            entry["error"] = "Tool reported an error result."
            return entry, "failed"
        return entry, "completed"

    @staticmethod
    def _parse_tool_proposal(tc: object) -> tuple[str, dict[str, object], str | None]:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"].strip():
            return "unknown", {}, "Invalid tool call."
        name = fn["name"].strip().lower()
        raw = fn.get("arguments", {})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return name, {"invalid_arguments": raw}, "Arguments must be a valid JSON object."
        if not isinstance(raw, dict):
            return name, {}, "Arguments must be a JSON object."
        return name, raw, None

    def _append_tool_result(self, messages: list[dict[str, object]], tc: dict[str, object], result: dict[str, object]) -> None:
        messages.append(self._assistant_tool_call_message(tc))
        messages.append({"role": "tool", "tool_call_id": str(tc.get("id", "unknown")),
                         "content": json.dumps(result, ensure_ascii=False, default=str)})

    def _persist_execution_receipt(self, run: AgentRun, ledger: ToolLoopLedger) -> None:
        if not isinstance(run, AgentRun):
            return
        self.db.execute(update(AgentRun).where(
            AgentRun.run_id == run.run_id, AgentRun.status == "running",
        ).values(execution_receipt_json=self._to_json(ledger.receipt()))
          .execution_options(synchronize_session=False))
        self.db.commit()
        self.db.refresh(run)

    def _load_execution_receipt(self, run: AgentRun) -> AgentExecutionReceipt | None:
        return AgentExecutionReceipt.from_snapshot(
            getattr(run, "execution_receipt_json", None), run_status=getattr(run, "status", "running"),
        )

    def _can_fallback_tool_loop(self, run: AgentRun) -> bool:
        receipt = self._load_execution_receipt(run)
        # An unreadable existing receipt is not evidence that nothing executed.
        if getattr(run, "execution_receipt_json", None):
            return receipt is not None and receipt.tool_calls_used == 0
        return True

    def _requires_tool_loop_checkpoint(self, run: AgentRun) -> bool:
        if run.execution_receipt_json is not None:
            return True
        # A non-loop approval needs positive provenance too. In particular,
        # deleting a loop checkpoint must not turn its consent into fallback.
        plans = sorted((step for step in self._load_steps(run.run_id) if step.step_type == "plan"),
                       key=lambda step: (step.created_at, step.step_id), reverse=True)
        for step in plans:
            output = self._safe_json_dict(step.output_json)
            if output.get("strategy") == "keyword_fallback" or "planned_tool_calls" in output:
                return False
            if output.get("strategy") == "llm_function_calling":
                return True
            if self._load_request_payload(run).run_mode.strip().lower() == "workflow":
                return False
        return True

    def _restore_execution_ledger(self, run: AgentRun, state: dict[str, Any], pending_call: AgentToolCall) -> ToolLoopLedger:
        """Resume the same reservations, including pre-ledger approval records.

        Legacy messages are execution transcripts, not instructions. Only paired
        assistant/tool messages can establish consumed budget; an unmatched final
        call must be the exact operation currently awaiting consent.
        """
        try:
            budget = int(run.max_steps)
            expected = self._tool_call_signature(pending_call.tool_name, pending_call.arguments)
            messages = state.get("messages")
            pending_id = state.get("pending_tool_call_id")
            if not isinstance(messages, list) or not isinstance(pending_id, str) or not pending_id:
                raise ValueError("Missing approval transcript.")
            if "execution_ledger" in state:
                ledger = ToolLoopLedger.from_state(state["execution_ledger"], budget=budget)
            else:
                ledger = ToolLoopLedger(budget=budget)
                unresolved = []
                for message in messages:
                    if not isinstance(message, dict):
                        raise ValueError("Invalid approval transcript.")
                    if message.get("role") == "assistant" and message.get("tool_calls"):
                        calls = message["tool_calls"]
                        if not isinstance(calls, list):
                            raise ValueError("Invalid historical tool calls.")
                        for call in calls:
                            name, args, error = self._parse_tool_proposal(call)
                            raw_effective = self._enrich_tool_call(
                                call=AgentToolCall(tool_name=name, arguments=args),
                                space_id=run.space_id, task=run.task,
                            ).arguments
                            if not error:
                                definition = self.tool_service.get_tool_definition(name)
                                args = definition.normalize_arguments(args) if definition else args
                                args = self._enrich_tool_call(
                                    call=AgentToolCall(tool_name=name, arguments=args),
                                    space_id=run.space_id, task=run.task,
                                ).arguments
                            signature = self._tool_call_signature(name, args)
                            if str(call.get("id", "")) == pending_id and self._tool_call_signature(name, raw_effective) == expected:
                                signature = expected
                            if not ledger.reserve(signature):
                                raise ValueError("Legacy approval exceeds the original budget.")
                            unresolved.append((str(call.get("id", "")), signature))
                    elif message.get("role") == "tool":
                        match = next((i for i, item in enumerate(unresolved)
                                      if item[0] == message.get("tool_call_id")), None)
                        if match is None:
                            raise ValueError("Unpaired historical tool result.")
                        content = message.get("content")
                        result = json.loads(content) if isinstance(content, str) else content
                        if not isinstance(result, dict) or not ({"result", "error"} & set(result)):
                            raise ValueError("Unverifiable historical result.")
                        outcome = ("rejected" if result.get("rejected") else "failed") if "error" in result else (
                            "skipped" if isinstance(result.get("result"), dict) and result["result"].get("dry_run") else
                            "failed" if self._tool_value_failed(result.get("result")) else "completed")
                        ledger.resolve(outcome)
                        unresolved.pop(match)
                if unresolved != [(pending_id, expected)]:
                    raise ValueError("Legacy pending operation is ambiguous.")
            if ledger.pending != 1 or not ledger.history or ledger.history[-1] != expected:
                raise ValueError("Approval reservation does not match the current operation.")
            # The checkpoint and the reviewed operation must name the same call.
            last = messages[-1] if messages else None
            calls = last.get("tool_calls") if isinstance(last, dict) and last.get("role") == "assistant" else None
            if not isinstance(calls, list) or len(calls) != 1 or str(calls[0].get("id", "")) != pending_id:
                raise ValueError("Approval call id does not match the checkpoint.")
            name, args, error = self._parse_tool_proposal(calls[0])
            raw_effective = self._enrich_tool_call(
                call=AgentToolCall(tool_name=name, arguments=args),
                space_id=run.space_id, task=run.task,
            ).arguments
            definition = self.tool_service.get_tool_definition(name)
            args = definition.normalize_arguments(args) if definition else args
            # Context enrichment (e.g. resolved space_id) is frozen in the
            # reviewed call, so compare the effective arguments, not guesses.
            normalized = self._enrich_tool_call(
                call=AgentToolCall(tool_name=name, arguments=args),
                space_id=run.space_id, task=run.task,
            ).arguments
            if error or expected not in {
                self._tool_call_signature(name, normalized), self._tool_call_signature(name, raw_effective),
            }:
                raise ValueError("Approval arguments do not match the checkpoint.")
            return ledger
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DomainValidationError(
                "本次待确认任务的工具预算记录不完整或已超出原预算，暂不执行。请取消后新建任务。"
            ) from exc

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
                run_id=run.run_id, step_index=step_index,
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
        self._record_step(
            run=run, step_index=self._next_step_index(run.run_id), step_type="plan",
            title="Build execution plan (fallback)", status="completed",
            input_payload={"task": run.task},
            output_payload={"strategy": "keyword_fallback",
                            "planned_tool_calls": [call.model_dump() for call in planned_calls]},
        )
        tool_results, skipped_calls, tool_errors = [], [], []
        if planned_calls:
            step_index = self._next_step_index(run.run_id)
            tool_results, skipped_calls, tool_errors, _ = self._execute_tool_calls(
                calls=planned_calls, team_id=team_id, user_id=user_id,
                space_id=space_id, task=run.task,
                dry_run=run.dry_run, confirm_dangerous_actions=False,
                run_id=run.run_id, step_index=step_index,
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
    # CLI agent mode (Claude Code / Codex)
    # ------------------------------------------------------------------

    def _run_cli_agent(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
    ) -> AgentRunResponse:
        try:
            for _event in self._run_cli_agent_stream(
                run=run, payload=payload, team_id=team_id, user_id=user_id,
                space_id=space_id, started=started,
            ):
                pass
        except Exception as exc:
            # 启动/配置类错误可能在完成前抛出；保证 run 不会停留在 running。
            if run.status in ("queued", "running"):
                self._finish_run(run=run, status="failed",
                                 final_answer=f"CLI agent 执行失败：{exc}",
                                 required_confirmations=[], started=started)
            raise
        return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

    def _run_cli_agent_stream(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
    ) -> Iterator[AgentStreamEvent]:
        settings = self._frozen_cli_settings or get_settings().model_copy(deep=True)
        if not settings.cli_agent_enabled:
            raise DomainValidationError(
                "CLI agent 后端未启用，请在服务端设置 CLI_AGENT_ENABLED=true 后重启。"
            )

        mode = payload.run_mode.strip().lower()
        runner = create_cli_agent_runner(mode, settings)
        workdir = resolve_cli_workspace(
            settings, team_id=team_id,
            conversation_id=payload.conversation_id, run_id=run.run_id,
        )
        identity = self._safe_json_dict(run.execution_identity_json) if run.execution_identity_json else build_cli_execution_identity(
            settings, mode, system_prompt=payload.system_prompt or "", requested_model=payload.model, dry_run=payload.dry_run,
        )
        session_store = CliSessionStore(workdir)
        dispatch = session_store.begin_dispatch(runner.name, identity["binding"], run_id=run.run_id)
        resume_session_id = dispatch.resume_session_id
        max_output = max(200, int(settings.cli_agent_max_step_output_chars))

        try:
            items = self._conversation_history(team_id=team_id, user_id=user_id, conversation_id=payload.conversation_id)
            seen_messages, native_runs = set(dispatch.consumed_message_ids), set(dispatch.native_run_ids)
            fresh_items = [item for item in items if (
                str(getattr(item, "message_id", "")) not in seen_messages
                and self._safe_json_dict(getattr(item, "response_payload_json", "{}")).get("run_id") not in native_runs
            )]
            context = build_conversation_context(
                fresh_items, max_turns=max(0, int(settings.llm_history_turns)), max_chars=settings.agent_context_max_chars,
            )
            cli_context = render_cli_context(context)
            self._record_context_snapshot(
                run, context, continuity="native_resume" if resume_session_id else "fresh",
                reset_reason=dispatch.fresh_reason, native_checkpoint=False,
            )
            effective_task = (cli_context.text + "\n\n" if cli_context.text else "") + payload.task
            seq = 0

            def _event(name: str, step_index: int | None, data: dict[str, object]) -> AgentStreamEvent:
                nonlocal seq
                seq += 1
                return AgentStreamEvent(
                    event=name, run_id=run.run_id, step_index=step_index, seq=seq, payload=data,
                )

            yield _event("step.started", 1, {
                "step_type": "plan",
                "title": f"委派 {runner.display_name} 执行",
            })
            self._record_step(
                run=run, step_index=1, step_type="plan",
                title=f"委派 {runner.display_name} 执行", status="completed",
                input_payload={
                    "task": payload.task, "run_mode": mode,
                    "workdir": str(workdir), "resume_session": bool(resume_session_id),
                },
                output_payload={"strategy": "cli_agent", "runner": runner.name},
            )

            answer_parts: list[str] = []
            session_id = resume_session_id or ""
            step_index = 2  # 工具步骤从 3 开始，与内置循环保持一致
            open_tools: dict[str, tuple[str, dict[str, object], int, float]] = {}
            tool_order: list[str] = []
            result_ok: bool | None = None
            result_text = ""
            error_text = ""

            cli_events = runner.run(
                task=effective_task,
                system_prompt=payload.system_prompt or "",
                workdir=workdir,
                resume_session_id=resume_session_id,
                dry_run=payload.dry_run,
            )
            try:
                for cli_event in cli_events:
                    if cli_event.kind == "session":
                        session_id = cli_event.session_id or session_id
                        # A session exists, but no completed delivery receipt
                        # exists until the CLI returns a successful result.
                        if cli_event.session_id:
                            session_store.note_session(dispatch, cli_event.session_id)
                    elif cli_event.kind == "text":
                        if cli_event.text:
                            answer_parts.append(cli_event.text)
                            yield _event("llm.delta", None, {"text": cli_event.text})
                    elif cli_event.kind == "tool_use":
                        step_index += 1
                        key = cli_event.tool_id or f"auto-{step_index}"
                        open_tools[key] = (
                            cli_event.tool_name, cli_event.tool_input, step_index, time.perf_counter(),
                        )
                        tool_order.append(key)
                        yield _event("tool.started", step_index, {"tool_name": cli_event.tool_name})
                    elif cli_event.kind == "tool_result":
                        key = cli_event.tool_id if cli_event.tool_id in open_tools else (
                            tool_order[0] if tool_order else ""
                        )
                        tool_name, tool_input, tool_step, tool_started = open_tools.pop(
                            key, ("tool", {}, step_index, time.perf_counter()),
                        )
                        if key in tool_order:
                            tool_order.remove(key)
                        output_text = self._truncate_cli_text(cli_event.tool_output, max_output)
                        self._record_step(
                            run=run, step_index=tool_step, step_type="tool_call",
                            title=f"{runner.display_name}: {tool_name}",
                            status="completed" if cli_event.ok else "failed",
                            tool_name=tool_name[:64],
                            input_payload={"arguments": self._truncate_cli_payload(tool_input, max_output)},
                            output_payload={"result": output_text},
                            latency_ms=self._elapsed_ms(tool_started),
                        )
                        yield _event("tool.result", tool_step, {
                            "tool_name": tool_name, "result": output_text,
                        })
                    elif cli_event.kind == "warning":
                        if cli_event.text:
                            self._record_step(
                                run=run, step_index=step_index + 1, step_type="observation",
                                title=f"{runner.display_name} 提示", status="completed",
                                input_payload={},
                                output_payload={"message": self._truncate_cli_text(cli_event.text, max_output)},
                            )
                            step_index += 1
                    elif cli_event.kind == "result":
                        result_ok = cli_event.ok
                        result_text = cli_event.text
                        session_id = cli_event.session_id or session_id
                        if cli_event.session_id:
                            session_store.note_session(dispatch, cli_event.session_id)
                    elif cli_event.kind == "error":
                        result_ok = False
                        error_text = cli_event.text
            finally:
                cli_events.close()

            # 仍未闭合的工具调用（CLI 提前结束 / 超时被杀）补记为 skipped，
            # 使持久化轨迹与流式事件对齐。
            for key in list(tool_order):
                tool_name, tool_input, tool_step, tool_started = open_tools.pop(key)
                self._record_step(
                    run=run, step_index=tool_step, step_type="tool_call",
                    title=f"{runner.display_name}: {tool_name}", status="skipped",
                    tool_name=tool_name[:64],
                    input_payload={"arguments": self._truncate_cli_payload(tool_input, max_output)},
                    output_payload={"result": "未收到工具结果（CLI 已结束）"},
                    latency_ms=self._elapsed_ms(tool_started),
                )
            tool_order.clear()

            final_answer = (result_text or "").strip() or "".join(answer_parts).strip()
            final_index = step_index + 50

            if result_ok is not True:
                detail = (error_text or result_text or f"{runner.display_name} 未返回可确认完成的结果").strip()
                if not final_answer:
                    final_answer = detail
                self._record_step(
                    run=run, step_index=final_index, step_type="final",
                    title=f"{runner.display_name} 执行失败", status="failed",
                    input_payload={"task": payload.task},
                    output_payload={"error": self._truncate_cli_text(detail, max_output)},
                    error_message=detail[:500],
                )
                self._finish_run(run=run, status="failed", final_answer=final_answer,
                                 required_confirmations=[], started=started)
                yield _event("run.failed", None, {"error": self._truncate_cli_text(detail, max_output)})
                return

            if self._is_run_cancelled(run):
                return
            checkpoint = session_store.commit_success(
                dispatch, message_ids=cli_context.selected_ids,
                history_boundary=cli_context.selected_ids[-1] if cli_context.selected_ids else None,
            )
            self._record_context_snapshot(
                run, context, continuity="native_resume" if resume_session_id else "fresh",
                reset_reason=dispatch.fresh_reason, native_checkpoint=checkpoint,
            )
            status = "completed"
            self._record_step(
                run=run, step_index=final_index, step_type="final",
                title="Summarize result", status=status,
                input_payload={"task": payload.task},
                output_payload={"answer": final_answer, "status": status, "cli_session_id": session_id},
            )
            yield _event("step.completed", final_index, {"answer": final_answer})
            self._run_plugin_phase("AfterReasoning", run.run_id, team_id, user_id, space_id,
                                   run=run, final_answer=final_answer)
            self._finish_run(run=run, status=status, final_answer=final_answer,
                             required_confirmations=[], started=started)
            self._run_plugin_phase("AfterTurn", run.run_id, team_id, user_id, space_id, run=run)
        finally:
            dispatch.close()

    @staticmethod
    def _truncate_cli_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n[... 已截断，共 {len(text)} 字符 ...]"

    @classmethod
    def _truncate_cli_payload(cls, payload: object, max_chars: int) -> object:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return {"raw": cls._truncate_cli_text(str(payload), max_chars)}
        if len(encoded) <= max_chars:
            return payload
        return {"raw": cls._truncate_cli_text(encoded, max_chars)}

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
                    run_id=run.run_id, step_index=step_index,
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

    def _run_plugin_phase(self, phase: str, run_id: str, team_id: str, user_id: str, space_id: str | None, **slots):
        """Run plugin phase hooks if plugin_manager is available."""
        if self.plugin_manager is None:
            return None
        from app.plugins.base import PhaseFrame
        frame = PhaseFrame(run_id=run_id, team_id=team_id, user_id=user_id, space_id=space_id, slots=dict(slots))
        return self.plugin_manager.run_phase(phase, frame)

    def _build_memory_prompt_blocks(
        self,
        *,
        team_id: str,
        user_id: str,
        space_id: str | None,
        include_memory: bool = True,
    ) -> str:
        """Return MEMORY.md + RECENT_CONTEXT.md prompt injection block."""
        settings = get_settings()
        if not include_memory or not settings.memory_markdown_enabled or self.memory_store is None:
            return ""
        if not team_id or not user_id:
            return ""

        parts: list[str] = []
        long_term = self.memory_store.read_long_term(team_id, user_id, space_id)
        if long_term:
            parts.append(f"## Long-term Memory\n\n{long_term[:6000]}")

        recent = self.memory_store.read_recent_context(team_id, user_id, space_id, include_recent_turns=False)
        if recent:
            parts.append(f"## Recent Context\n\n{recent[:3000]}")

        return "\n\n".join(parts)

    def _build_agent_messages(
        self,
        *,
        task: str,
        system_prompt: str | None,
        rag_answer: str,
        team_id: str = "",
        user_id: str = "",
        space_id: str | None = None,
        conversation_id: str | None = None,
        include_memory: bool = True,
        channel: str | None = None,
        run: AgentRun | None = None,
    ) -> list[dict[str, object]]:
        persona = self.persona_prompt_builder.build(
            system_prompt=system_prompt,
            channel=channel,
            team_id=team_id, user_id=user_id, space_id=space_id,
            include_memory=include_memory,
        )

        time_envelope = build_current_message_time_envelope()
        history = self._conversation_context(self._conversation_history(
            team_id=team_id, user_id=user_id, conversation_id=conversation_id,
        ))
        history_messages = history.messages
        if run is not None:
            self._record_context_snapshot(run, history, continuity="api_history")
        messages: list[dict[str, object]] = [
            {"role": "system", "content": persona.system_prompt},
        ]
        messages.extend(history_messages)
        if rag_answer.strip():
            messages.append({
                "role": "assistant",
                "content": f"Retrieved workspace context: {rag_answer[:2000]}",
            })
        messages.append({"role": "user", "content": f"{time_envelope}\n{task}"})
        return messages

    def _conversation_history(self, *, team_id: str, user_id: str, conversation_id: str | None):
        if not team_id or not user_id or not conversation_id or self.llm_service.settings.llm_history_turns < 1:
            return []
        # Fetch a bounded formal history before applying mode-specific receipts.
        # Never use a timestamp alone to skip equal-time messages.
        return self.chat_history_service.list_messages_for_context(
            team_id=team_id, user_id=user_id, conversation_id=conversation_id, limit=100,
        )

    def _conversation_context(self, items):
        settings = self.llm_service.settings
        return build_conversation_context(
            items, max_turns=max(0, int(settings.llm_history_turns)),
            max_chars=int(getattr(settings, "agent_context_max_chars", 24000)),
        )

    def _build_agent_conversation_messages(
        self, *, team_id: str, user_id: str, conversation_id: str | None,
    ) -> list[dict[str, object]]:
        return list(self._conversation_context(self._conversation_history(
            team_id=team_id, user_id=user_id, conversation_id=conversation_id,
        )).messages)

    def _record_context_snapshot(self, run: AgentRun, context, *, continuity: str,
                                 reset_reason: str | None = None, **extra) -> None:
        snapshot = {
            "version": 1, "source": "caibao.chat_history",
            "selected_message_ids": list(context.selected_ids),
            "selected_count": len(context.selected_ids), "omitted_count": context.omitted_count,
            "truncated_count": context.truncated_count, "excluded_count": context.excluded_count,
            "history_enabled": self.llm_service.settings.llm_history_turns > 0,
            "max_chars": context.max_chars, "continuity": continuity,
            "reset_reason": reset_reason, **extra,
        }
        run.context_snapshot_json = self._to_json(snapshot)
        self.db.add(run)
        self.db.commit()

    def _build_tools(
        self,
        *,
        payload: AgentRunRequest | None,
        run: AgentRun,
    ) -> list[dict[str, object]]:
        enabled_tools = payload.enabled_tools if payload is not None else None
        return self.tool_service.catalog.get_openai_tools(enabled_tools=enabled_tools)

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

    def _prepare_execution(self, run: AgentRun, payload: AgentRunRequest) -> dict[str, object]:
        mode = payload.run_mode.strip().lower()
        if mode in CLI_AGENT_MODES:
            # Freeze one settings object for identity, session binding and the
            # actual command. API routing does not participate in CLI dispatch.
            self._frozen_cli_settings = get_settings().model_copy(deep=True)
            identity = build_cli_execution_identity(
                self._frozen_cli_settings, mode, system_prompt=payload.system_prompt or "",
                requested_model=payload.model, dry_run=payload.dry_run,
            )
            run.model_routes_json = None
            routes = {}
        else:
            routes = self._resolve_routes(team_id=run.team_id, user_id=run.user_id, payload=payload)
            identity = self._api_execution_identity(routes, payload)
            self._persist_routes(run, routes)
        run.execution_identity_json = self._to_json(identity)
        run.model = identity.get("model")
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return routes

    def _api_execution_identity(self, routes: dict[str, object], payload: AgentRunRequest) -> dict[str, object]:
        planner = self._routes_to_planner_dict(routes)
        provider = "openai_compatible" if planner["base_url"] else self.llm_service.settings.llm_provider
        return build_api_execution_identity(routes, requested_model=payload.model, provider=provider)

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
        for route in snapshot.values():
            if isinstance(route, dict):
                route["base_url"] = safe_execution_endpoint(route.get("base_url")) or ""
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

    @staticmethod
    def _tool_budget_prompt(tool_budget: int) -> str:
        return (
            f"Tool-call budget reached ({tool_budget}). Do not call any more tools. "
            "Use the available tool results and answer the user's request now. "
            "If the evidence is incomplete, say that briefly."
        )

    @staticmethod
    def _normalize_tool_call_for_message(tool_call: dict[str, object]) -> dict[str, object]:
        public_tool_call = {
            key: value
            for key, value in tool_call.items()
            if not str(key).startswith("_assistant_")
        }
        normalized = json.loads(json.dumps(public_tool_call, ensure_ascii=False, default=str))
        if not isinstance(normalized, dict):
            return {}
        function = normalized.get("function")
        if isinstance(function, dict):
            arguments = function.get("arguments")
            if arguments is None:
                function["arguments"] = "{}"
            elif not isinstance(arguments, str):
                function["arguments"] = json.dumps(arguments, ensure_ascii=False, default=str)
            normalized["function"] = function
        return normalized

    @classmethod
    def _assistant_tool_call_message(cls, tool_call: dict[str, object]) -> dict[str, object]:
        message: dict[str, object] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [cls._normalize_tool_call_for_message(tool_call)],
        }
        reasoning_content = tool_call.get("_assistant_reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            message["reasoning_content"] = reasoning_content
        return message

    def _count_consecutive(self, history: list[tuple[str, str]], sig: tuple[str, str]) -> int:
        count = 0
        for item in reversed(history):
            if item == sig:
                count += 1
            else:
                break
        return count

    def _save_loop_state(self, run: AgentRun, *, messages: list[dict[str, object]], pending_tool_call_id: str, ledger: ToolLoopLedger | None = None) -> None:
        loop = {
            "messages": self._strip_non_serializable_messages(messages),
            "pending_tool_call_id": pending_tool_call_id,
            **({"execution_ledger": ledger.to_state()} if ledger is not None else {}),
        }
        self.db.execute(
            update(AgentRun).where(AgentRun.run_id == run.run_id, AgentRun.status.not_in(("cancelled", "interrupted")))
            .values(loop_state_json=self._to_json(loop)).execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.refresh(run)

    def _load_loop_state(self, run: AgentRun) -> dict[str, Any] | None:
        if not run.loop_state_json:
            return None
        return self._safe_json_dict(run.loop_state_json)

    def _save_pending_tool_call(self, run: AgentRun, call: AgentToolCall) -> None:
        self.db.execute(
            update(AgentRun).where(AgentRun.run_id == run.run_id, AgentRun.status.not_in(("cancelled", "interrupted")))
            .values(required_confirmations_json=self._to_json([call.model_dump()]))
            .execution_options(synchronize_session=False)
        )
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
            if not isinstance(content, str) and content is not None:
                c["content"] = json.dumps(content, ensure_ascii=False, default=str)
            if "tool_calls" in c and c["tool_calls"] is not None:
                tool_calls = c["tool_calls"]
                if isinstance(tool_calls, str):
                    tool_calls = json.loads(tool_calls)
                else:
                    tool_calls = json.loads(json.dumps(tool_calls, ensure_ascii=False, default=str))
                if isinstance(tool_calls, list):
                    c["tool_calls"] = [
                        self._normalize_tool_call_for_message(tool_call)
                        if isinstance(tool_call, dict)
                        else tool_call
                        for tool_call in tool_calls
                    ]
                else:
                    c["tool_calls"] = tool_calls
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
        run_id: str = "",
        step_index: int = 0,
    ) -> tuple[list[dict[str, object]], list[AgentToolCall], list[str], str]:
        tool_results: list[dict[str, object]] = []
        skipped_calls: list[AgentToolCall] = []
        tool_errors: list[str] = []

        if dry_run:
            return [], [call.model_copy(update={"requires_confirmation": call.dangerous}) for call in calls], [], "skipped"

        for call in calls:
            if run_id and self.db.scalar(select(AgentRun.status).where(AgentRun.run_id == run_id)) == "cancelled":
                break
            enriched_call = self._enrich_tool_call(call=call, space_id=space_id, task=task)
            if enriched_call.dangerous and not confirm_dangerous_actions:
                skipped_calls.append(enriched_call.model_copy(update={"requires_confirmation": True}))
                continue
            if self.event_bus:
                self.event_bus.emit(ToolCalled(
                    run_id=run_id, step_index=step_index,
                    tool_name=enriched_call.tool_name,
                    arguments=enriched_call.arguments,
                ))
            try:
                result = self.tool_service.execute(
                    team_id=team_id, user_id=user_id,
                    action=enriched_call.tool_name,
                    arguments=enriched_call.arguments,
                    confirmed=True,
                )
                tool_results.append({
                    "tool_name": enriched_call.tool_name,
                    "arguments": enriched_call.arguments,
                    "result": result,
                })
                if self.event_bus:
                    self.event_bus.emit(ToolResulted(
                        run_id=run_id, step_index=step_index,
                        tool_name=enriched_call.tool_name, status="completed",
                    ))
            except Exception as exc:  # noqa: BLE001
                tool_errors.append(f"{enriched_call.tool_name}: {exc}")
                if self.event_bus:
                    self.event_bus.emit(ToolResulted(
                        run_id=run_id, step_index=step_index,
                        tool_name=enriched_call.tool_name, status="failed",
                        error=str(exc),
                    ))

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
            client_request_id=payload.client_request_id,
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
        """Replay persisted state without starting another execution."""
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
        event_name = (
            "run.cancelled" if full_response.status == "cancelled"
            else "run.in_progress" if full_response.status in {"queued", "running"}
            else "run.completed"
        )
        yield AgentStreamEvent(
            event=event_name, run_id=run.run_id, seq=seq,
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

        if self.event_bus:
            self.event_bus.emit(StepCompleted(
                run_id=run.run_id, step_index=step_index,
                step_type=step_type, status=status,
                latency_ms=latency_ms,
            ))
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
        values: dict[str, Any] = {
            "status": status, "final_answer": final_answer,
            "required_confirmations_json": self._to_json([item.model_dump() for item in required_confirmations]),
            "latency_ms": self._elapsed_ms(started), "completed_at": datetime.now(timezone.utc),
        }
        if status != "requires_confirmation":
            values["loop_state_json"] = None
        # A result arriving after Stop must not revive the run or its approvals.
        changed = self.db.execute(
            update(AgentRun).where(AgentRun.run_id == run.run_id, AgentRun.status.not_in(("cancelled", "interrupted")))
            .values(**values).execution_options(synchronize_session=False)
        ).rowcount
        self.db.commit()
        self.db.refresh(run)
        if not changed:
            return

        if self.event_bus and status in ("completed", "partial_success"):
            self.event_bus.enqueue(TurnCommitted(
                run_id=run.run_id,
                team_id=run.team_id,
                user_id=run.user_id,
                conversation_id=run.conversation_id,
                space_id=run.space_id,
                input_message=run.task,
                assistant_response=run.final_answer,
                tools_used=self._extract_tool_names(run),
                channel=run.trigger_channel,
            ))

    def _extract_tool_names(self, run: AgentRun) -> list[str]:
        stmt = select(AgentStep).where(
            AgentStep.run_id == run.run_id,
            AgentStep.step_type == "tool_call",
        ).order_by(AgentStep.step_index)
        steps = list(self.db.scalars(stmt).all())
        tool_names: list[str] = []
        for s in steps:
            if s.tool_name and s.tool_name not in tool_names:
                tool_names.append(s.tool_name)
        return tool_names

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
        effective_channel = (payload.trigger_channel or "agent").strip().lower() or "agent"
        candidates = self.db.scalars(select(ChatHistory).where(
            ChatHistory.team_id == response.team_id, ChatHistory.user_id == response.user_id,
            ChatHistory.channel == effective_channel,
            ChatHistory.response_payload_json.contains(response.run_id),
        ))
        for item in candidates:
            if self._safe_json_dict(item.response_payload_json).get("run_id") == response.run_id:
                item.response_text = response.answer
                item.response_payload_json = self._to_json(response.model_dump(mode="json"))
                self.db.commit()
                return
        self.chat_history_service.record_message(
            team_id=response.team_id, user_id=response.user_id,
            conversation_id=response.conversation_id, channel=effective_channel,
            request_text=payload.task, response_text=response.answer,
            request_payload=payload.model_dump(),
            response_payload=response.model_dump(mode="json"),
        )

    def _ensure_run_access(self, *, run_id: str, team_id: str, user_id: str) -> AgentRun:
        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        run = self.db.scalar(
            select(AgentRun).where(AgentRun.run_id == run_id)
            .execution_options(populate_existing=True)
        )
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
