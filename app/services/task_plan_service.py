"""Persist bounded task-plan snapshots without executing their contents."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.agent_run import AgentRun
from app.schemas.task_plan import AgentTaskPlan, TaskPlanSource


class TaskPlanService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def load(self, run: AgentRun) -> AgentTaskPlan | None:
        """Read the actual persisted snapshot, including after a lost CAS."""
        with self.db.no_autoflush:
            raw = self.db.scalar(select(AgentRun.task_plan_json).where(
                AgentRun.run_id == run.run_id, AgentRun.team_id == run.team_id,
                AgentRun.user_id == run.user_id,
            ))
        return AgentTaskPlan.from_snapshot(raw)

    def update(
        self, run: AgentRun, *, source: TaskPlanSource, steps: list[dict[str, object]],
        explanation: str | None = None,
    ) -> AgentTaskPlan | None:
        """CAS one active run's metadata; return None when nothing was written.

        The first 64 different valid snapshots are retained normally. A 65th
        different snapshot stores a limit marker while keeping revision 64's
        contents. Source changes and unreadable existing state fail closed.
        """
        with self.db.no_autoflush:
            current = self.db.execute(select(AgentRun.status, AgentRun.task_plan_json).where(
                AgentRun.run_id == run.run_id, AgentRun.team_id == run.team_id,
                AgentRun.user_id == run.user_id,
            )).one_or_none()
        if current is None:
            raise EntityNotFoundError(f"Agent run '{run.run_id}' not found.")
        status, previous_raw = current
        if status != "running":
            return None
        previous = AgentTaskPlan.from_snapshot(previous_raw)
        if previous_raw is not None and previous is None:
            raise DomainValidationError("The current task plan cannot be verified; it will not be overwritten.")
        if previous is not None and previous.source != source:
            raise DomainValidationError("Task plan source cannot change within a run.")
        if previous is not None and previous.limited:
            return None
        try:
            candidate = AgentTaskPlan.model_validate({
                "version": 1, "revision": 1 if previous is None else previous.revision,
                "source": source, "model_reported": True, "steps": steps,
                "explanation": explanation, "limited": False,
            })
        except ValidationError as exc:
            raise DomainValidationError("Invalid task plan: use at most ten short, uniquely identified steps and one active step.") from exc
        if previous is not None:
            if candidate.steps == previous.steps and candidate.explanation == previous.explanation:
                return None
            candidate = candidate.model_copy(update={"revision": previous.revision + 1})
            if previous.revision == 64:
                candidate = previous.model_copy(update={"revision": 65, "limited": True})
        changed = self.db.execute(update(AgentRun).where(
            AgentRun.run_id == run.run_id, AgentRun.team_id == run.team_id,
            AgentRun.user_id == run.user_id, AgentRun.status == "running",
            AgentRun.task_plan_json == previous_raw,
        ).values(task_plan_json=candidate.model_dump_json())
          .execution_options(synchronize_session=False)).rowcount
        self.db.commit()
        self.db.refresh(run, attribute_names=["status", "task_plan_json"])
        return candidate if changed else None
