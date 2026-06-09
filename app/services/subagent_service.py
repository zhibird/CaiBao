from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.subagent_job import SubAgentJob

_logger = logging.getLogger(__name__)

_PROFILE_TOOLS = {
    "research": ["web_search", "web_fetch", "bilibili_latest_videos", "search_knowledge", "read_file"],
    "ops": ["search_knowledge", "list_recent_documents", "generate_incident_report"],
}


class SubAgentService:
    """Spawn sub-agents with profile-restricted tool sets."""

    def __init__(self, db: Session, llm_service=None, tool_service=None) -> None:
        self.db = db
        self._llm = llm_service
        self._tool_svc = tool_service

    def run_sync(
        self,
        *,
        profile: str,
        task: str,
        team_id: str,
        user_id: str,
        space_id: str | None = None,
        max_steps: int = 10,
        model_name: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        """Run a sub-agent synchronously. Returns result dict."""
        tool_names = _PROFILE_TOOLS.get(profile, ["search_knowledge"])
        settings = get_settings()
        current_model = model_name or settings.llm_model or ""
        current_base = base_url or settings.llm_base_url or ""
        current_key = api_key or settings.llm_api_key or ""

        tools = self._build_tools(tool_names)
        messages = [
            {"role": "system", "content": f"You are a sub-agent with profile '{profile}'. Complete the task using available tools. Return a concise result."},
            {"role": "user", "content": task},
        ]

        result_text = ""
        for step in range(max_steps):
            try:
                llm_result = self._llm.complete_chat(
                    messages=messages, model=current_model,
                    base_url=current_base, api_key=current_key, tools=tools,
                )
            except Exception as exc:
                return {"status": "error", "error": str(exc), "steps": step + 1}

            if not llm_result.tool_calls:
                result_text = llm_result.assistant_text or ""
                break

            for tc in llm_result.tool_calls:
                fn = tc.get("function", {})
                t_name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}

                tool_result = self._execute_tool(t_name, args, team_id, user_id)
                messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                messages.append({"role": "tool", "tool_call_id": str(tc.get("id", "")), "content": json.dumps(tool_result, ensure_ascii=False, default=str)})

        if not result_text:
            result_text = "Sub-agent completed without a final response."

        return {"status": "completed", "result": result_text, "profile": profile, "steps": min(step + 1, max_steps)}

    def run_async(
        self,
        *,
        profile: str,
        task: str,
        team_id: str,
        user_id: str,
        parent_run_id: str = "",
        max_steps: int = 15,
    ) -> str:
        """Create an async sub-agent job. Returns job_id."""
        job = SubAgentJob(
            job_id=str(uuid4()),
            parent_run_id=parent_run_id or None,
            team_id=team_id,
            user_id=user_id,
            profile=profile,
            task=task,
            status="queued",
            max_steps=max_steps,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        def _run():
            from app.db.session import SessionLocal
            from app.services.llm_service import LLMService
            from app.services.tool_catalog_service import ToolCatalogService
            from app.services.tool_safety import ToolSafetyService
            from app.services.tool_service import ToolService
            db2 = SessionLocal()
            try:
                job2 = db2.get(SubAgentJob, job.job_id)
                if job2 is None:
                    return
                job2.status = "running"
                db2.add(job2)
                db2.commit()

                # Reconstruct full tool chain with the worker's own DB session.
                # Register generic tools so profiles can use web_search, web_fetch, etc.
                catalog2 = ToolCatalogService()
                from app.services.tools.web_tools import create_web_tools
                from app.services.tools.file_tools import create_file_tools
                catalog2.register_generic(create_web_tools())
                catalog2.register_generic(create_file_tools())

                safety2 = ToolSafetyService()
                tool_svc2 = ToolService(db=db2, catalog=catalog2, safety=safety2)

                # Register generic handlers for the tools
                from app.services.tools.web_tools import (
                    bilibili_latest_videos_handler,
                    web_fetch_handler,
                    web_search_handler,
                )
                from app.services.tools.file_tools import list_dir_handler, read_file_handler, write_file_handler, edit_file_handler
                tool_svc2.register_generic_handler("web_fetch", web_fetch_handler)
                tool_svc2.register_generic_handler("web_search", web_search_handler)
                tool_svc2.register_generic_handler("bilibili_latest_videos", bilibili_latest_videos_handler)
                tool_svc2.register_generic_handler("list_dir", list_dir_handler)
                tool_svc2.register_generic_handler("read_file", read_file_handler)
                tool_svc2.register_generic_handler("write_file", write_file_handler)
                tool_svc2.register_generic_handler("edit_file", edit_file_handler)
                llm2 = LLMService(settings=get_settings())
                svc2 = SubAgentService(db=db2, llm_service=llm2, tool_service=tool_svc2)
                result = svc2.run_sync(profile=profile, task=task, team_id=team_id, user_id=user_id, max_steps=max_steps)
                job2.status = result.get("status", "completed")
                job2.result_json = json.dumps(result, ensure_ascii=False, default=str)
                job2.completed_at = datetime.now(timezone.utc)
                db2.add(job2)
                db2.commit()
            except Exception as exc:
                _logger.exception("Async subagent job %s failed", job.job_id)
                try:
                    job2 = db2.get(SubAgentJob, job.job_id)
                    if job2:
                        job2.status = "failed"
                        job2.result_json = json.dumps({"error": str(exc)})
                        job2.completed_at = datetime.now(timezone.utc)
                        db2.add(job2)
                        db2.commit()
                except Exception:
                    pass
            finally:
                db2.close()

        t = threading.Thread(target=_run, daemon=True, name=f"subagent-{job.job_id[:8]}")
        t.start()
        return job.job_id

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_tools(self, tool_names: list[str]) -> list[dict]:
        if self._tool_svc is None:
            _logger.warning("SubAgent has no ToolService — tools will be empty for profile")
            return []
        from app.services.tool_registry import ToolDefinition
        tools = []
        for name in tool_names:
            d = self._tool_svc.get_tool_definition(name)
            if d is not None:
                tools.append(d.to_openai_tool())
        return tools

    def _execute_tool(self, tool_name: str, args: dict, team_id: str, user_id: str) -> dict:
        if self._tool_svc is None:
            return {"error": "ToolService not available"}
        try:
            return self._tool_svc.execute(
                team_id=team_id, user_id=user_id,
                action=tool_name, arguments=args, confirmed=True,
            )
        except Exception as exc:
            return {"error": str(exc)}
