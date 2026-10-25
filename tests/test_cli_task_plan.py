"""CLI plan projections from documented JSONL shapes, without CLI/model calls.

Contracts: openai/codex SDK ThreadItem/ThreadEvent and JSONL processor;
https://code.claude.com/docs/en/agent-sdk/python#todowrite and
https://code.claude.com/docs/en/agent-sdk/todo-tracking.
"""

import json

import pytest

from app.core.config import Settings
from app.services.cli_agent_service import ClaudeCodeRunner, CodexRunner, PiRunner


def _runner(kind=ClaudeCodeRunner):
    return kind(Settings(_env_file=None))


def _parse(runner, event):
    return runner.parse_line(json.dumps(event, ensure_ascii=False))


def _plans(events):
    return [event for event in events if event.kind == "plan"]


def _use(runner, identifier, name, arguments, **extra):
    return _parse(runner, {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": identifier, "name": name, "input": arguments,
    }]}, **extra})


def _result(runner, identifier, output=None, *, error=False, **extra):
    return _parse(runner, {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": identifier, "is_error": error,
        "content": [{"type": "text", "text": "tool returned"}],
    }]}, "tool_use_result": output, **extra})


def _create(runner, identifier, *, subject="核对资料", call_id="create-1"):
    _use(runner, call_id, "TaskCreate", {"subject": subject, "description": "PRIVATE DESCRIPTION"})
    return _result(runner, call_id, {"task": {"id": identifier, "subject": subject}})


def _update(runner, identifier, *, call_id="update-1", status="in_progress", subject=None):
    arguments = {"taskId": identifier}
    if status is not None:
        arguments["status"] = status
    if subject is not None:
        arguments["subject"] = subject
    _use(runner, call_id, "TaskUpdate", arguments)
    return _result(runner, call_id, {
        "success": True, "taskId": identifier, "updatedFields": list(set(arguments) - {"taskId"}),
        "statusChange": {"from": "pending", "to": status} if status is not None else None,
    })


@pytest.mark.parametrize("event_type", ["item.started", "item.updated", "item.completed"])
def test_codex_projects_each_todo_lifecycle_without_inventing_in_progress_or_completion(event_type):
    events = _parse(_runner(CodexRunner), {"type": event_type, "item": {
        "id": "native-item-private", "type": "todo_list", "items": [
            {"text": "核对资料", "completed": True}, {"text": "整理结果", "completed": False},
        ],
    }})
    assert len(events) == 1
    assert events[0].plan_source == "codex.todo_list"
    assert events[0].plan_steps == [
        {"id": "s1", "content": "核对资料", "status": "completed"},
        {"id": "s2", "content": "整理结果", "status": "pending"},
    ]
    assert events[0].tool_input == {} and events[0].session_id == ""


@pytest.mark.parametrize("items", [
    None, {}, "[]", [None], [{"text": "a", "completed": "false"}],
    [{"text": "a", "completed": 0}], [{"text": "a"}],
    [{"text": "", "completed": False}], [{"text": "   ", "completed": False}],
    [{"text": "x" * 161, "completed": True}], [{"text": [], "completed": True}],
    [{"text": "a", "completed": False}] * 11,
])
def test_codex_rejects_the_whole_invalid_snapshot_instead_of_publishing_a_subset(items):
    assert _parse(_runner(CodexRunner), {"type": "item.updated", "item": {"type": "todo_list", "items": items}}) == []


def test_codex_empty_list_clears_plan_but_reasoning_and_text_are_not_parsed_as_plans():
    runner = _runner(CodexRunner)
    assert _parse(runner, {"type": "item.updated", "item": {"type": "todo_list", "items": []}})[0].plan_steps == []
    assert _parse(runner, {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE THOUGHTS"}}) == []
    events = _parse(runner, {"type": "item.completed", "item": {"type": "agent_message", "text": "1. done"}})
    assert [event.kind for event in events] == ["text"]
    _parse(runner, {"type": "turn.failed", "error": {"message": "stopped"}})
    assert _parse(runner, {"type": "item.updated", "item": {"type": "todo_list", "items": []}}) == []


def test_claude_todowrite_waits_for_success_and_exposes_only_bounded_display_fields():
    runner = _runner()
    todos = [{"content": "核对资料", "status": "in_progress", "activeForm": "PRIVATE ACTIVE FORM"}]
    assert [event.kind for event in _use(runner, "todo-1", "TodoWrite", {"todos": todos})] == ["tool_use"]
    events = _result(runner, "todo-1", {"newTodos": todos, "raw_error": "PRIVATE ERROR"})
    assert [event.kind for event in events] == ["tool_result", "plan"]
    assert events[-1].plan_source == "claude.TodoWrite"
    assert events[-1].plan_steps == [{"id": "s1", "content": "核对资料", "status": "in_progress"}]
    assert "PRIVATE" not in repr(events[-1])


@pytest.mark.parametrize("todos", [
    None, {}, "[]", [None], [{"content": "a", "status": []}],
    [{"content": "a", "status": "cancelled"}], [{"content": "a", "status": "unknown"}],
    [{"content": "x" * 161, "status": "pending"}], [{"content": " ", "status": "pending"}],
    [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}],
    [{"content": "a", "status": "pending"}] * 11,
])
def test_claude_todowrite_invalid_snapshot_never_becomes_a_plan(todos):
    runner = _runner()
    _use(runner, "bad-1", "TodoWrite", {"todos": todos})
    assert _plans(_result(runner, "bad-1", {"newTodos": todos})) == []


@pytest.mark.parametrize("error", [True, "false", 0, None])
def test_claude_failed_or_non_boolean_result_flag_never_confirms_metadata(error):
    runner = _runner()
    _use(runner, "todo-1", "TodoWrite", {"todos": []})
    assert _plans(_result(runner, "todo-1", error=error)) == []


def test_claude_todowrite_prefers_successful_actual_output_and_does_not_fallback_from_invalid_output():
    runner = _runner()
    _use(runner, "todo-1", "TodoWrite", {"todos": [{"content": "原提议", "status": "pending"}]})
    plan = _plans(_result(runner, "todo-1", {"newTodos": [{"content": "实际返回", "status": "completed"}]}))[0]
    assert plan.plan_steps[0]["content"] == "实际返回"
    _use(runner, "todo-2", "TodoWrite", {"todos": []})
    assert _plans(_result(runner, "todo-2", {"newTodos": "invalid"})) == []
    _use(runner, "todo-3", "TodoWrite", {"todos": []})
    assert _plans(_result(runner, "todo-3"))[0].plan_steps == []


def test_claude_tasks_use_real_create_output_ids_then_support_paired_status_rename_and_delete():
    runner = _runner()
    first = _plans(_create(runner, "native-task-secret"))[0]
    assert first.plan_source == "claude.tasks"
    assert first.plan_steps == [{"id": "s1", "content": "核对资料", "status": "pending"}]
    assert "native-task-secret" not in repr(first)
    active = _plans(_update(runner, "native-task-secret"))[0]
    assert active.plan_steps[0]["status"] == "in_progress"
    renamed = _plans(_update(runner, "native-task-secret", call_id="rename", status=None, subject="核对另一份资料"))[0]
    assert renamed.plan_steps[0] == {"id": "s1", "content": "核对另一份资料", "status": "in_progress"}
    done = _plans(_update(runner, "native-task-secret", call_id="done", status="completed"))[0]
    assert done.plan_steps[0]["status"] == "completed"
    assert first.plan_steps[0]["status"] == "pending", "later registry mutations cannot rewrite earlier emitted snapshots"
    assert _plans(_update(runner, "native-task-secret", call_id="delete", status="deleted"))[0].plan_steps == []


@pytest.mark.parametrize("output", [None, {}, {"task": {}}, {"task": {"id": 1}}, {"task": {"id": "x" * 65}}])
def test_claude_task_create_never_extracts_or_invents_an_id_from_request_or_result_text(output):
    runner = _runner()
    _use(runner, "create-1", "TaskCreate", {"subject": "核对资料", "id": "untrusted-input-id"})
    assert _plans(_result(runner, "create-1", output)) == []
    assert _plans(_update(runner, "untrusted-input-id")) == []


@pytest.mark.parametrize("output", [
    None, {}, {"success": False, "taskId": "actual", "updatedFields": ["status"]},
    {"success": True, "taskId": "another", "updatedFields": ["status"]},
    {"success": True, "taskId": "actual", "updatedFields": []},
    {"success": True, "taskId": "actual", "updatedFields": ["status"], "error": "operation failed"},
    {"success": True, "taskId": "actual", "updatedFields": ["status"], "statusChange": {"to": "completed"}},
])
def test_claude_task_update_needs_structured_success_for_the_same_task_and_fields(output):
    runner = _runner()
    _create(runner, "actual")
    _use(runner, "update-1", "TaskUpdate", {"taskId": "actual", "status": "in_progress"})
    assert _plans(_result(runner, "update-1", output)) == []
    changed = _plans(_update(runner, "actual", call_id="genuine", status="completed"))[0]
    assert changed.plan_steps[0]["status"] == "completed"


@pytest.mark.parametrize("alias", ["taskId", "id", "task_id"])
def test_claude_known_task_updates_accept_documented_raw_key_aliases(alias):
    runner = _runner()
    _create(runner, "actual")
    _use(runner, "update-1", "TaskUpdate", {alias: "actual", "status": "completed"})
    events = _result(runner, "update-1", {"success": True, "taskId": "actual", "updatedFields": ["status"]})
    assert _plans(events)[0].plan_steps[0]["status"] == "completed"


def test_claude_out_of_order_results_are_correlated_and_projected_in_original_call_order():
    runner = _runner()
    _use(runner, "create-a", "TaskCreate", {"subject": "先核对"})
    _use(runner, "create-b", "TaskCreate", {"subject": "再整理"})
    assert _plans(_result(runner, "create-b", {"task": {"id": "task-b"}})) == []
    events = _plans(_result(runner, "create-a", {"task": {"id": "task-a"}}))
    assert len(events) == 2
    assert events[-1].plan_steps == [
        {"id": "s1", "content": "先核对", "status": "pending"},
        {"id": "s2", "content": "再整理", "status": "pending"},
    ]
    assert _plans(_result(runner, "create-a", {"task": {"id": "duplicate"}})) == []
    assert _plans(_update(runner, "unknown-task")) == []


def test_claude_duplicate_calls_cannot_rewrite_a_snapshot_or_change_its_source():
    runner = _runner()
    _create(runner, "actual")
    _use(runner, "create-1", "TaskCreate", {"subject": "重复事件不能改写"})
    assert _plans(_result(runner, "create-1", {"task": {"id": "another"}})) == []
    _use(runner, "todo-1", "TodoWrite", {"todos": []})
    assert _plans(_result(runner, "todo-1")) == []
    assert _plans(_update(runner, "actual"))[0].plan_steps[0]["content"] == "核对资料"


def test_claude_todowrite_source_cannot_switch_to_tasks_mid_run():
    runner = _runner()
    _use(runner, "todo-1", "TodoWrite", {"todos": []})
    assert _plans(_result(runner, "todo-1"))[0].plan_source == "claude.TodoWrite"
    assert _plans(_create(runner, "actual")) == []


def test_claude_ambiguous_message_scoped_create_output_is_not_assigned_to_multiple_calls():
    runner = _runner()
    for identifier in ("a", "b"):
        _use(runner, identifier, "TaskCreate", {"subject": identifier})
    events = _parse(runner, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "a"}, {"type": "tool_result", "tool_use_id": "b"},
    ]}, "tool_use_result": {"task": {"id": "ambiguous"}}})
    assert _plans(events) == []


def test_claude_oversize_registry_is_never_truncated_and_can_recover_after_verified_delete():
    runner = _runner()
    for index in range(10):
        assert len(_plans(_create(runner, str(index), call_id=f"c{index}"))[-1].plan_steps) == index + 1
    assert _plans(_create(runner, "eleventh", call_id="c11")) == []
    assert _plans(_update(runner, "0", call_id="u0", status="completed")) == []
    remaining = _plans(_update(runner, "eleventh", call_id="delete11", status="deleted"))[0]
    assert len(remaining.plan_steps) == 10 and remaining.plan_steps[0]["status"] == "completed"


def test_claude_multiple_active_items_are_not_silently_reduced_to_a_single_active_item():
    runner = _runner()
    _create(runner, "a", call_id="ca")
    _create(runner, "b", call_id="cb")
    _update(runner, "a", call_id="ua")
    assert _plans(_update(runner, "b", call_id="ub")) == []
    recovered = _plans(_update(runner, "a", call_id="finish-a", status="completed"))[0]
    assert [item["status"] for item in recovered.plan_steps] == ["completed", "in_progress"]


def test_claude_registry_is_dispatch_local_and_does_not_read_subagents_or_late_terminal_results():
    runner = _runner()
    _create(runner, "actual")
    assert _plans(_update(_runner(), "actual")) == []
    runner._reset_parser_state()
    assert _plans(_update(runner, "actual", call_id="after-reset")) == []
    _use(runner, "sub", "TodoWrite", {"todos": []}, parent_tool_use_id="subagent")
    assert _plans(_result(runner, "sub", parent_tool_use_id="subagent")) == []
    _use(runner, "late", "TodoWrite", {"todos": []})
    _parse(runner, {"type": "result", "result": "ended", "is_error": False})
    assert _plans(_result(runner, "late")) == []


@pytest.mark.parametrize("kind", [ClaudeCodeRunner, CodexRunner, PiRunner])
def test_unknown_metadata_shapes_and_plain_prose_never_invent_a_plan_or_crash(kind):
    runner = _runner(kind)
    for event in [
        {"type": []}, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": {}, "input": {}}]}},
        {"type": "task_notification", "status": "completed"},
        {"type": "plan", "steps": [{"content": "not a documented event", "status": "completed"}]},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "PRIVATE"}},
    ]:
        assert _plans(_parse(runner, event)) == []
    assert runner.parse_line("1. completed a task") == []
