from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.agent_run import AgentRun
from app.models.agent_step import AgentStep
from app.schemas.agent import AgentRunRequest, AgentRunResponse, AgentStepResponse, AgentToolCall
from app.schemas.chat import ChatAskRequest, ChatSource
from app.services.chat_history_service import ChatHistoryService
from app.services.document_service import DocumentService
from app.services.rag_chat_service import RagChatService
from app.services.tool_service import ToolService
from app.services.user_service import UserService


class AgentService:
    """Single-agent executor built on existing RAG, memory, tool and trace services."""

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
    ) -> None:
        self.db = db
        self.user_service = user_service
        self.document_service = document_service
        self.rag_chat_service = rag_chat_service
        self.tool_service = tool_service
        self.chat_history_service = chat_history_service

    def run(self, payload: AgentRunRequest) -> AgentRunResponse:
        team_id = str(payload.team_id or "").strip()
        user_id = str(payload.user_id or "").strip()
        if not team_id or not user_id:
            raise DomainValidationError("team_id and user_id are required.")

        self.user_service.ensure_user_in_team(user_id=user_id, team_id=team_id)
        effective_space_id = self.document_service.resolve_space_id(
            team_id=team_id,
            conversation_id=payload.conversation_id,
            space_id=payload.space_id,
            user_id=user_id,
        )

        started = time.perf_counter()
        run = self._create_run(payload=payload, team_id=team_id, user_id=user_id, space_id=effective_space_id)
        if payload.run_mode.strip().lower() == "workflow" and self._workflow_nodes(payload.workflow_config):
            response = self._run_workflow(
                run=run,
                payload=payload,
                team_id=team_id,
                user_id=user_id,
                space_id=effective_space_id,
                started=started,
            )
        else:
            response = self._run_agent_auto(
                run=run,
                payload=payload,
                team_id=team_id,
                user_id=user_id,
                space_id=effective_space_id,
                started=started,
            )
        self._record_agent_history(payload=payload, response=response)
        return response

    def confirm_run(self, *, run_id: str, team_id: str, user_id: str) -> AgentRunResponse:
        run = self._ensure_run_access(run_id=run_id, team_id=team_id, user_id=user_id)
        if run.status != "requires_confirmation":
            raise DomainValidationError("Only runs waiting for confirmation can be confirmed.")

        pending_calls = self._load_required_confirmations(run)
        if not pending_calls:
            raise DomainValidationError("This run has no pending tool calls.")

        started = time.perf_counter()
        next_index = self._next_step_index(run.run_id)
        tool_results, _, tool_errors, tool_status = self._execute_tool_calls(
            calls=pending_calls,
            team_id=team_id,
            user_id=user_id,
            space_id=run.space_id,
            task=run.task,
            dry_run=False,
            confirm_dangerous_actions=True,
        )
        self._record_step(
            run=run,
            step_index=next_index,
            step_type="tool_call",
            title="Execute confirmed tools",
            status=tool_status,
            tool_name=",".join(call.tool_name for call in pending_calls),
            input_payload={"calls": [call.model_dump() for call in pending_calls], "confirmed": True},
            output_payload={"executed": tool_results, "errors": tool_errors},
            error_message="; ".join(tool_errors) or None,
            latency_ms=self._elapsed_ms(started),
        )

        final_status = "failed" if tool_errors and not tool_results else ("partial_success" if tool_errors else "completed")
        final_answer = self._build_final_answer(
            status=final_status,
            task=run.task,
            rag_answer=run.final_answer,
            tool_results=tool_results,
            skipped_calls=[],
            tool_errors=tool_errors,
            dry_run=False,
            confirmed=True,
        )
        self._record_step(
            run=run,
            step_index=next_index + 1,
            step_type="final",
            title="Summarize confirmed result",
            status=final_status,
            input_payload={"run_id": run_id},
            output_payload={"answer": final_answer, "status": final_status},
        )
        self._finish_run(
            run=run,
            status=final_status,
            final_answer=final_answer,
            required_confirmations=[],
            started=started,
        )
        return self.get_run(run_id=run_id, team_id=team_id, user_id=user_id)

    def get_run(self, *, run_id: str, team_id: str, user_id: str) -> AgentRunResponse:
        run = self._ensure_run_access(run_id=run_id, team_id=team_id, user_id=user_id)
        steps = self._load_steps(run.run_id)
        return AgentRunResponse(
            run_id=run.run_id,
            team_id=run.team_id,
            user_id=run.user_id,
            conversation_id=run.conversation_id,
            space_id=run.space_id,
            app_id=run.app_id,
            app_version=run.app_version,
            trigger_channel=run.trigger_channel,
            task=run.task,
            status=run.status,
            answer=run.final_answer,
            model=run.model,
            dry_run=run.dry_run,
            max_steps=run.max_steps,
            required_confirmations=self._load_required_confirmations(run),
            steps=[AgentStepResponse.from_record(item) for item in steps],
            tool_calls=self._extract_tool_calls_from_steps(steps),
            sources=self._extract_sources_from_steps(steps),
            latency_ms=run.latency_ms,
            created_at=run.created_at,
            completed_at=run.completed_at,
        )

    def list_tools(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self.tool_service.list_tools()]

    def _run_agent_auto(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
    ) -> AgentRunResponse:
        planned_calls = self._plan_tool_calls(payload.task, enabled_tools=payload.enabled_tools, space_id=space_id)
        step_index = 1
        self._record_step(
            run=run,
            step_index=step_index,
            step_type="plan",
            title="Build execution plan",
            status="completed",
            input_payload={
                "task": payload.task,
                "max_steps": payload.max_steps,
                "run_mode": payload.run_mode,
                "enabled_tools": payload.enabled_tools,
            },
            output_payload={
                "planned_tool_calls": [item.model_dump() for item in planned_calls],
                "strategy": "retrieval_first_then_tools",
            },
        )

        step_index += 1
        rag_answer, retrieval_failed = self._record_retrieval_step(
            run=run,
            payload=payload,
            team_id=team_id,
            user_id=user_id,
            space_id=space_id,
            step_index=step_index,
            title="Retrieve workspace context",
        )
        if retrieval_failed is not None:
            self._finish_run(
                run=run,
                status="failed",
                final_answer=f"Agent 检索阶段失败：{retrieval_failed}",
                required_confirmations=[],
                started=started,
            )
            return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

        tool_results: list[dict[str, object]] = []
        skipped_calls: list[AgentToolCall] = []
        tool_errors: list[str] = []
        if planned_calls:
            step_index += 1
            tool_started = time.perf_counter()
            tool_results, skipped_calls, tool_errors, tool_status = self._execute_tool_calls(
                calls=planned_calls,
                team_id=team_id,
                user_id=user_id,
                space_id=space_id,
                task=payload.task,
                dry_run=payload.dry_run,
                confirm_dangerous_actions=payload.confirm_dangerous_actions,
            )
            self._record_step(
                run=run,
                step_index=step_index,
                step_type="tool_call",
                title="Execute selected tools",
                status=tool_status,
                tool_name=",".join(call.tool_name for call in planned_calls),
                input_payload={"calls": [call.model_dump() for call in planned_calls]},
                output_payload={
                    "executed": tool_results,
                    "skipped": [call.model_dump() for call in skipped_calls],
                    "errors": tool_errors,
                },
                error_message="; ".join(tool_errors) or None,
                latency_ms=self._elapsed_ms(tool_started),
            )

        step_index += 1
        status = self._resolve_run_status(
            dry_run=payload.dry_run,
            skipped_calls=skipped_calls,
            tool_errors=tool_errors,
            tool_results=tool_results,
        )
        final_answer = self._build_final_answer(
            status=status,
            task=payload.task,
            rag_answer=rag_answer,
            tool_results=tool_results,
            skipped_calls=skipped_calls,
            tool_errors=tool_errors,
            dry_run=payload.dry_run,
        )
        self._record_step(
            run=run,
            step_index=step_index,
            step_type="final",
            title="Summarize result",
            status=status,
            input_payload={"task": payload.task},
            output_payload={
                "answer": final_answer,
                "status": status,
                "tool_result_count": len(tool_results),
                "required_confirmation_count": len(skipped_calls),
            },
        )
        self._finish_run(
            run=run,
            status=status,
            final_answer=final_answer,
            required_confirmations=skipped_calls,
            started=started,
        )
        return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

    def _run_workflow(
        self,
        *,
        run: AgentRun,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
        started: float,
    ) -> AgentRunResponse:
        nodes = self._workflow_nodes(payload.workflow_config)
        self._record_step(
            run=run,
            step_index=1,
            step_type="plan",
            title="Load workflow plan",
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
                    run=run,
                    payload=payload,
                    team_id=team_id,
                    user_id=user_id,
                    space_id=space_id,
                    step_index=step_index,
                    title=title,
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
                    calls=[call],
                    team_id=team_id,
                    user_id=user_id,
                    space_id=space_id,
                    task=payload.task,
                    dry_run=payload.dry_run,
                    confirm_dangerous_actions=payload.confirm_dangerous_actions,
                )
                tool_results.extend(call_results)
                skipped_calls.extend(call_skipped)
                tool_errors.extend(call_errors)
                self._record_step(
                    run=run,
                    step_index=step_index,
                    step_type="tool_call",
                    title=title,
                    status=tool_status,
                    tool_name=call.tool_name,
                    input_payload={"calls": [call.model_dump()]},
                    output_payload={
                        "executed": call_results,
                        "skipped": [item.model_dump() for item in call_skipped],
                        "errors": call_errors,
                    },
                    error_message="; ".join(call_errors) or None,
                )
                continue

            if node_type in {"condition", "llm"}:
                self._record_step(
                    run=run,
                    step_index=step_index,
                    step_type="observation",
                    title=title,
                    status="completed",
                    input_payload=node,
                    output_payload={
                        "note": "Lightweight MVP records this node for trace and continues sequentially.",
                    },
                )
                continue

            if node_type == "final":
                self._record_step(
                    run=run,
                    step_index=step_index,
                    step_type="observation",
                    title=title,
                    status="completed",
                    input_payload=node,
                    output_payload={"note": "Final node reached."},
                )
                break

        final_status = self._resolve_run_status(
            dry_run=payload.dry_run,
            skipped_calls=skipped_calls,
            tool_errors=tool_errors,
            tool_results=tool_results,
        )
        final_answer = self._build_final_answer(
            status=final_status,
            task=payload.task,
            rag_answer=rag_answer,
            tool_results=tool_results,
            skipped_calls=skipped_calls,
            tool_errors=tool_errors,
            dry_run=payload.dry_run,
        )
        self._record_step(
            run=run,
            step_index=step_index + 1,
            step_type="final",
            title="Summarize workflow result",
            status=final_status,
            input_payload={"task": payload.task},
            output_payload={"answer": final_answer, "status": final_status},
        )
        self._finish_run(
            run=run,
            status=final_status,
            final_answer=final_answer,
            required_confirmations=skipped_calls,
            started=started,
        )
        return self.get_run(run_id=run.run_id, team_id=team_id, user_id=user_id)

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
                    user_id=user_id,
                    team_id=team_id,
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
        except Exception as exc:  # noqa: BLE001 - traces should retain upstream failure detail
            self._record_step(
                run=run,
                step_index=step_index,
                step_type="retrieval",
                title=title,
                status="failed",
                input_payload={"top_k": payload.top_k},
                output_payload={},
                error_message=str(exc),
                latency_ms=self._elapsed_ms(retrieval_started),
            )
            return "", str(exc)

        self._record_step(
            run=run,
            step_index=step_index,
            step_type="retrieval",
            title=title,
            status="completed",
            input_payload={
                "top_k": payload.top_k,
                "include_memory": payload.include_memory,
                "include_library": payload.include_library,
                "include_conclusions": payload.include_conclusions,
            },
            output_payload={
                "mode": rag_response.mode,
                "answer_preview": rag_response.answer[:500],
                "source_count": len(rag_response.sources),
                "sources": [item.model_dump() for item in rag_response.sources],
            },
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
                    team_id=team_id,
                    user_id=user_id,
                    action=enriched_call.tool_name,
                    arguments=enriched_call.arguments,
                )
                tool_results.append(
                    {
                        "tool_name": enriched_call.tool_name,
                        "arguments": enriched_call.arguments,
                        "result": result,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                tool_errors.append(f"{enriched_call.tool_name}: {exc}")

        if tool_errors:
            return tool_results, skipped_calls, tool_errors, "partial_success" if tool_results else "failed"
        if skipped_calls:
            return tool_results, skipped_calls, tool_errors, "requires_confirmation"
        return tool_results, skipped_calls, tool_errors, "completed"

    def _create_run(
        self,
        *,
        payload: AgentRunRequest,
        team_id: str,
        user_id: str,
        space_id: str | None,
    ) -> AgentRun:
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
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

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
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

    def _plan_tool_calls(
        self,
        task: str,
        *,
        enabled_tools: list[str] | None,
        space_id: str | None,
    ) -> list[AgentToolCall]:
        normalized = task.strip()
        lowered = normalized.lower()
        calls: list[AgentToolCall] = []

        if self._should_search_knowledge(lowered):
            calls.append(
                self._build_tool_call(
                    tool_name="search_knowledge",
                    arguments={"query": normalized, "limit": self._extract_limit(lowered), "space_id": space_id},
                )
            )
        if self._should_list_recent_documents(lowered):
            calls.append(
                self._build_tool_call(
                    tool_name="list_recent_documents",
                    arguments={"limit": self._extract_limit(lowered), "space_id": space_id},
                )
            )
        if self._should_create_incident(normalized, lowered):
            calls.append(
                self._build_tool_call(
                    tool_name="create_incident",
                    arguments={
                        "title": self._extract_incident_title(normalized),
                        "severity": self._extract_severity(normalized),
                    },
                )
            )
        if self._should_create_memory(lowered):
            calls.append(
                self._build_tool_call(
                    tool_name="create_memory_card",
                    arguments={
                        "space_id": space_id,
                        "title": self._extract_title(normalized, fallback="Agent Memory"),
                        "content": normalized,
                        "category": "agent",
                    },
                )
            )
        if self._should_promote_conclusion(lowered):
            calls.append(
                self._build_tool_call(
                    tool_name="promote_to_conclusion",
                    arguments={
                        "space_id": space_id,
                        "title": self._extract_title(normalized, fallback="Agent Conclusion"),
                        "content": normalized,
                        "topic": "agent",
                        "status": "draft",
                    },
                )
            )
        if self._should_generate_report(lowered):
            calls.append(
                self._build_tool_call(
                    tool_name="generate_incident_report",
                    arguments={
                        "title": self._extract_title(normalized, fallback="Incident Report"),
                        "incident_summary": normalized,
                    },
                )
            )

        return self._filter_enabled_tools(calls, enabled_tools=enabled_tools)

    def _build_tool_call(self, *, tool_name: str, arguments: dict[str, object]) -> AgentToolCall:
        definition = self.tool_service.get_tool_definition(tool_name)
        dangerous = bool(definition.dangerous) if definition is not None else False
        clean_args = {key: value for key, value in arguments.items() if value is not None}
        return AgentToolCall(
            tool_name=tool_name,
            arguments=clean_args,
            dangerous=dangerous,
            requires_confirmation=dangerous,
        )

    def _tool_call_from_workflow_node(
        self,
        *,
        node: dict[str, Any],
        space_id: str | None,
        task: str,
    ) -> AgentToolCall:
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

    def _filter_enabled_tools(
        self,
        calls: list[AgentToolCall],
        *,
        enabled_tools: list[str] | None,
    ) -> list[AgentToolCall]:
        if enabled_tools is None:
            return calls
        allowed = {item.strip().lower() for item in enabled_tools if str(item).strip()}
        return [call for call in calls if call.tool_name.strip().lower() in allowed]

    def _should_search_knowledge(self, lowered: str) -> bool:
        keywords = ["search knowledge", "knowledge base", "runbook", "知识库", "处理手册", "手册", "查资料", "检索资料"]
        return any(keyword in lowered for keyword in keywords)

    def _should_list_recent_documents(self, lowered: str) -> bool:
        keywords = [
            "recent document",
            "recent documents",
            "list documents",
            "latest document",
            "最近文档",
            "最近的文档",
            "最近导入",
            "查看文档",
            "列出文档",
            "查文档",
        ]
        return any(keyword in lowered for keyword in keywords)

    def _should_create_incident(self, task: str, lowered: str) -> bool:
        if "不要创建" in task or "do not create" in lowered:
            return False
        keywords = [
            "create incident",
            "open incident",
            "incident",
            "创建 incident",
            "创建一个 incident",
            "创建事件",
            "新建事件",
            "创建故障",
            "告警",
            "故障",
            "事故",
        ]
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

    def _resolve_run_status(
        self,
        *,
        dry_run: bool,
        skipped_calls: list[AgentToolCall],
        tool_errors: list[str],
        tool_results: list[dict[str, object]],
    ) -> str:
        if dry_run:
            return "completed"
        if skipped_calls:
            return "requires_confirmation"
        if tool_errors:
            return "partial_success" if tool_results else "failed"
        return "completed"

    def _build_final_answer(
        self,
        *,
        status: str,
        task: str,
        rag_answer: str,
        tool_results: list[dict[str, object]],
        skipped_calls: list[AgentToolCall],
        tool_errors: list[str],
        dry_run: bool,
        confirmed: bool = False,
    ) -> str:
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

    def _workflow_nodes(self, workflow_config: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = workflow_config.get("nodes") if isinstance(workflow_config, dict) else None
        if not isinstance(nodes, list):
            return []
        return [dict(node) for node in nodes if isinstance(node, dict)]

    def _record_agent_history(self, *, payload: AgentRunRequest, response: AgentRunResponse) -> None:
        self.chat_history_service.record_message(
            team_id=response.team_id,
            user_id=response.user_id,
            conversation_id=response.conversation_id,
            channel="agent",
            request_text=payload.task,
            response_text=response.answer,
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
            if not isinstance(raw_calls, list):
                continue
            for item in raw_calls:
                if isinstance(item, dict):
                    candidate = AgentToolCall.model_validate(item)
                    if all(existing.tool_name != candidate.tool_name or existing.arguments != candidate.arguments for existing in calls):
                        calls.append(candidate)
        return calls

    def _extract_sources_from_steps(self, steps: list[AgentStep]) -> list[ChatSource]:
        for step in steps:
            if step.step_type != "retrieval":
                continue
            output = self._safe_json_dict(step.output_json)
            raw_sources = output.get("sources")
            if not isinstance(raw_sources, list):
                continue
            sources: list[ChatSource] = []
            for item in raw_sources:
                if isinstance(item, dict):
                    sources.append(ChatSource.model_validate(item))
            return sources
        return []

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
