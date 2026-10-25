"""Task plans are bounded, model-reported snapshots, never execution proof."""

import json
from uuid import uuid4

from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.db.base import Base
from app.models.agent_run import AgentRun
from app.schemas.task_plan import AgentTaskPlan
from app.services.task_plan_service import TaskPlanService


def steps(content="Inspect the workspace", status="pending"):
    return [{"id": "inspect", "content": content, "status": status}]


def snapshot(**changes):
    return {"version": 1, "revision": 1, "source": "api.update_plan", "model_reported": True,
            "steps": steps(), "explanation": None, "limited": False, **changes}


@pytest.fixture
def plans(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plans.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        run = AgentRun(run_id=str(uuid4()), team_id="team", user_id="user", task="Do a task", status="running")
        db.add(run); db.commit()
        yield TaskPlanService(db), run, engine, sessions
    engine.dispose()


def test_update_and_reload_are_scoped_typed_and_plain_text(plans):
    service, run, _, _ = plans
    result = service.update(run, source="api.update_plan", steps=[{
        "id": " inspect ", "content": " <script>this remains text</script> ", "status": "in_progress",
    }], explanation="  A short public explanation  ")
    assert result is not None and result.revision == 1
    assert result.model_reported is True
    assert result.steps[0].id == "inspect"
    assert result.steps[0].content == "<script>this remains text</script>"
    assert result.explanation == "A short public explanation"
    assert service.load(run) == result
    assert AgentTaskPlan.from_snapshot(run.task_plan_json) == result
    assert run.status == "running"


def test_identical_normalized_snapshots_do_not_consume_revisions(plans):
    service, run, _, _ = plans
    first = service.update(run, source="api.update_plan", steps=steps())
    original = run.task_plan_json
    assert service.update(run, source="api.update_plan", steps=steps(" Inspect the workspace "), explanation=" ") is None
    assert run.task_plan_json == original and service.load(run).revision == first.revision


def test_source_changes_and_corrupt_existing_snapshots_are_rejected(plans):
    service, run, _, _ = plans
    service.update(run, source="api.update_plan", steps=steps())
    with pytest.raises(DomainValidationError, match="source"):
        service.update(run, source="codex.todo_list", steps=steps())
    original = run.task_plan_json
    service.db.execute(text("UPDATE agent_runs SET task_plan_json = :value"), {"value": "broken-json"})
    service.db.commit()
    assert service.load(run) is None
    with pytest.raises(DomainValidationError, match="verified"):
        service.update(run, source="api.update_plan", steps=steps("different"))
    assert original != "broken-json"
    assert service.db.scalar(text("SELECT task_plan_json FROM agent_runs")) == "broken-json"


def test_the_65th_distinct_revision_is_a_limit_marker_retaining_the_64th_contents(plans):
    service, run, _, _ = plans
    for number in range(1, 65):
        result = service.update(run, source="api.update_plan", steps=steps(f"Step version {number}"),
                                explanation=f"Explanation {number}")
        assert result.revision == number and result.limited is False
    assert service.update(run, source="api.update_plan", steps=steps("Step version 64"), explanation="Explanation 64") is None
    limited = service.update(run, source="api.update_plan", steps=steps("Never accepted"), explanation="Never shown")
    assert limited.revision == 65 and limited.limited is True
    assert limited.steps == result.steps and limited.explanation == "Explanation 64"
    assert service.update(run, source="api.update_plan", steps=steps("Ignored")) is None
    assert service.load(run) == limited


@pytest.mark.parametrize("status", ["queued", "requires_confirmation", "completed", "failed", "cancelled", "interrupted", "partial_success"])
def test_inactive_runs_do_not_accept_plan_updates_or_change_status(plans, status):
    service, run, _, _ = plans
    first = service.update(run, source="api.update_plan", steps=steps())
    service.db.execute(text("UPDATE agent_runs SET status = :status"), {"status": status}); service.db.commit()
    # The passed ORM object can still say running; the actual DB state wins.
    assert service.update(run, source="api.update_plan", steps=steps("late change")) is None
    assert service.load(run) == first
    assert service.db.scalar(text("SELECT status FROM agent_runs")) == status


def test_model_marking_every_step_complete_does_not_change_actual_run_outcome(plans):
    service, run, _, _ = plans
    result = service.update(run, source="api.update_plan", steps=steps(status="completed"))
    assert result.steps[0].status == "completed"
    assert run.status == "running" and run.final_answer == ""
    assert run.execution_receipt_json is None


def test_empty_plan_is_an_explicit_snapshot_and_its_explanation_can_be_cleared(plans):
    service, run, _, _ = plans
    service.update(run, source="api.update_plan", steps=steps(), explanation="Old explanation")
    cleared = service.update(run, source="api.update_plan", steps=[], explanation=None)
    assert cleared.revision == 2 and cleared.steps == [] and cleared.explanation is None


def test_update_loads_current_database_revision_even_with_a_stale_orm_object(plans):
    service, run, _, sessions = plans
    first = service.update(run, source="api.update_plan", steps=steps())
    with sessions() as other:
        other_run = other.get(AgentRun, run.run_id)
        second = TaskPlanService(other).update(other_run, source="api.update_plan", steps=steps("another owner"))
    assert run.task_plan_json == first.model_dump_json()
    third = service.update(run, source="api.update_plan", steps=steps("latest update"))
    assert second.revision == 2 and third.revision == 3


@pytest.mark.parametrize("race", ["cancel", "another-plan"])
def test_compare_and_swap_cannot_overwrite_a_concurrent_cancel_or_plan(plans, race):
    service, run, engine, sessions = plans
    winner = AgentTaskPlan.model_validate(snapshot(steps=steps("winning snapshot")))
    fired = []

    def race_before_update(connection, cursor, statement, parameters, context, executemany):
        if fired or not statement.startswith("UPDATE agent_runs SET task_plan_json="):
            return
        fired.append(True)
        with sessions() as other:
            if race == "cancel":
                other.execute(text("UPDATE agent_runs SET status = 'cancelled'"))
            else:
                other.execute(text("UPDATE agent_runs SET task_plan_json = :value"), {"value": winner.model_dump_json()})
            other.commit()

    event.listen(engine, "before_cursor_execute", race_before_update)
    try:
        assert service.update(run, source="api.update_plan", steps=steps("stale loser")) is None
    finally:
        event.remove(engine, "before_cursor_execute", race_before_update)
    assert fired == [True]
    if race == "cancel":
        assert run.status == "cancelled" and service.load(run) is None
    else:
        assert service.load(run) == winner


def test_wrong_identity_cannot_mutate_another_run(plans):
    service, run, _, _ = plans
    outsider = AgentRun(run_id=run.run_id, team_id="team", user_id="somebody-else", task="wrong identity", status="running")
    with pytest.raises(EntityNotFoundError):
        service.update(outsider, source="api.update_plan", steps=steps())
    assert service.load(run) is None and service.load(outsider) is None


@pytest.mark.parametrize("changes", [
    {"version": True}, {"revision": True}, {"revision": 0}, {"revision": 66},
    {"source": "unknown"}, {"model_reported": False}, {"model_reported": 1},
    {"explanation": "x" * 301}, {"limited": "false"}, {"limited": True},
    {"revision": 65}, {"internal_state": "private"},
    {"steps": steps() * 11}, {"steps": [{"id": " ", "content": "text", "status": "pending"}]},
    {"steps": [{"id": "x" * 65, "content": "text", "status": "pending"}]},
    {"steps": [{"id": "x", "content": " ", "status": "pending"}]},
    {"steps": [{"id": "x", "content": "x" * 161, "status": "pending"}]},
    {"steps": [{"id": "x", "content": 123, "status": "pending"}]},
    {"steps": [{"id": "x", "content": "text", "status": "done"}]},
    {"steps": [{"id": "x", "content": "text", "status": "pending", "command": "do not run"}]},
    {"steps": [{"id": " same ", "content": "A", "status": "pending"}, {"id": "same", "content": "B", "status": "completed"}]},
    {"steps": [{"id": "a", "content": "A", "status": "in_progress"}, {"id": "b", "content": "B", "status": "in_progress"}]},
])
def test_schema_rejects_oversize_ambiguous_or_private_metadata(changes):
    with pytest.raises(ValidationError):
        AgentTaskPlan.model_validate(snapshot(**changes))
    assert AgentTaskPlan.from_snapshot(json.dumps(snapshot(**changes))) is None


@pytest.mark.parametrize("raw", [None, "", "bad json", "null", "[]", "{}", 3, {}])
def test_snapshot_loader_is_safe_for_missing_and_malformed_values(raw):
    assert AgentTaskPlan.from_snapshot(raw) is None


def test_invalid_store_input_leaves_the_previous_snapshot_untouched(plans):
    service, run, _, _ = plans
    first = service.update(run, source="api.update_plan", steps=steps())
    with pytest.raises(DomainValidationError):
        service.update(run, source="api.update_plan", steps=steps("x" * 161))
    assert service.load(run) == first


def test_snapshot_loader_is_bounded_but_accepts_full_escaped_unicode_metadata():
    full = snapshot(
        steps=[{"id": f"{index}" + "彩" * 63, "content": "🐾" * 160, "status": "pending"} for index in range(10)],
        explanation="🐾" * 300,
    )
    assert AgentTaskPlan.from_snapshot(json.dumps(full)) is not None
    assert AgentTaskPlan.from_snapshot(" " * 64_001 + json.dumps(snapshot())) is None
