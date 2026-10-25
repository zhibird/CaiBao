"""Real admission/HTTP tests with isolated, scripted model replies only."""

import json

import pytest
from pydantic import ValidationError

from app.services.task_plan_control import MAX_MODEL_CALLS, PlanUpdateArguments, RunLoopControl
from tests.test_execution_receipt import (
    _call, _model_script, _receipt, _reply, _run, receipt_boundary,
)


def _plan(text="核对资料", status="in_progress", **changes):
    return _call("update_plan", {"plan": [{"step": text, "status": status}], **changes})


@pytest.mark.parametrize("streaming", [False, True])
def test_plan_updates_do_not_spend_external_budget_even_after_last_action(client, receipt_boundary, monkeypatch, streaming):
    requests = _model_script(monkeypatch, [
        _reply(_plan()),
        _reply(*[_call(arguments={"limit": value}, identifier=f"read-{value}") for value in (1, 3, 4)]),
        _reply(_plan(status="completed", explanation="三个读取结果均已返回。")),
        _reply(text="读取结果已经返回，请核对。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming,
               enabled_tools=["list_recent_documents", "update_plan"], max_steps=3)
    assert run["status"] == "completed", run
    assert run["task_plan"]["revision"] == 2
    assert run["task_plan"]["steps"][0]["status"] == "completed"
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 3
    assert receipt["omitted_tool_calls"] == receipt["tool_calls_rejected"] == 0
    assert len(receipt_boundary["attempted"]) == 3
    for request in requests[2:]:
        assert [item["function"]["name"] for item in request["tools"]] == ["update_plan"]
    assert len([step for step in run["steps"] if step["step_type"] == "task_plan"]) == 2
    assert "三个读取结果" in run["task_plan"]["explanation"]


@pytest.mark.parametrize("streaming", [False, True])
def test_plan_at_end_of_full_action_batch_is_not_omitted_external_work(client, receipt_boundary, monkeypatch, streaming):
    _model_script(monkeypatch, [
        _reply(*[_call(arguments={"limit": value}, identifier=f"read-{value}") for value in (1, 3, 4)], _plan(status="completed")),
        _reply(text="批次返回。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming,
               enabled_tools=["list_recent_documents", "update_plan"], max_steps=3)
    assert run["status"] == "completed", run
    assert run["task_plan"]["revision"] == 1
    assert _receipt(run)["omitted_tool_calls"] == 0


@pytest.mark.parametrize("streaming", [False, True])
def test_repeated_identical_updates_spend_finite_metadata_attempts_without_faking_actions(client, receipt_boundary, monkeypatch, streaming):
    requests = _model_script(monkeypatch, [
        *[_reply(_plan()) for _ in range(8)],
        _reply(_plan(text="不得写入第九次更新", status="completed")),
        _reply(text="保留未完步骤。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming, enabled_tools=["update_plan"])
    assert run["task_plan"]["revision"] == 1
    assert run["task_plan"]["steps"][0]["status"] == "in_progress"
    assert requests[8]["tools"] is None
    results = [json.loads(message["content"]) for message in requests[-1]["messages"] if message["role"] == "tool"]
    assert results[-2]["result"]["updates_remaining"] == 0
    assert results[-1]["rejected"] is True
    assert _receipt(run)["tool_calls_used"] == 0
    assert receipt_boundary["attempted"] == []


@pytest.mark.parametrize("arguments", [
    '"not an object"', '{broken', {"plan": [{"step": "", "status": "pending"}]},
    {"plan": [{"step": "A", "status": "completed", "reasoning": "private"}]},
    {"plan": [{"step": "A", "status": "in_progress"}, {"step": "B", "status": "in_progress"}]},
    {"plan": [{"step": "A", "status": "pending"}], "run_id": "another-run"},
])
def test_invalid_metadata_does_not_mutate_plan_or_external_receipt(client, receipt_boundary, monkeypatch, arguments):
    _model_script(monkeypatch, [_reply(_plan()), _reply(_call("update_plan", arguments)), _reply(text="未更新计划。")])
    run = _run(client, receipt_boundary, streaming=True, enabled_tools=["update_plan"])
    assert run["task_plan"]["revision"] == 1
    assert _receipt(run)["tool_calls_used"] == _receipt(run)["tool_calls_rejected"] == 0
    assert [step["status"] for step in run["steps"] if step["step_type"] == "task_plan"] == ["completed", "failed"]


def test_unbounded_planning_provider_stops_at_shared_model_limit(client, receipt_boundary, monkeypatch):
    requests = _model_script(monkeypatch, [_reply(_plan(text=f"版本{value}")) for value in range(30)])
    run = _run(client, receipt_boundary, streaming=True, enabled_tools=["update_plan"])
    assert len(requests) == MAX_MODEL_CALLS
    assert requests[-1]["tools"] is None
    assert run["task_plan"]["revision"] == 8
    assert run["status"] == "failed"
    assert _receipt(run)["stop_reason"] == "iteration_limit"
    assert _receipt(run)["tool_calls_used"] == _receipt(run)["omitted_tool_calls"] == 0


def test_plan_not_offered_in_chat_and_reserved_name_never_reaches_tool_executor(client, receipt_boundary, monkeypatch):
    requests = _model_script(monkeypatch, [_reply(_plan()), _reply(text="聊天回复。")])
    run = _run(client, receipt_boundary, streaming=True, enabled_tools=[])
    assert all(request["tools"] is None for request in requests)
    assert run["task_plan"] is None and _receipt(run)["tool_calls_used"] == 0
    assert receipt_boundary["attempted"] == []


def test_a_large_excess_metadata_batch_does_not_amplify_writes_or_drop_allowed_actions(client, receipt_boundary, monkeypatch):
    _model_script(monkeypatch, [
        _reply(*[_plan(text=f"更新{number}") for number in range(100)], _call(identifier="allowed-read")),
        _reply(text="查询已返回，计划额度已用完。"),
    ])
    run = _run(client, receipt_boundary, streaming=True, enabled_tools=["update_plan", "list_recent_documents"])
    assert run["task_plan"]["revision"] == 8
    assert len([step for step in run["steps"] if step["step_type"] == "task_plan"]) == 9
    assert _receipt(run)["tool_calls_used"] == _receipt(run)["tool_calls_succeeded"] == 1
    assert len(receipt_boundary["attempted"]) == 1


def test_simple_answer_does_not_force_a_plan_or_hide_current_user_task(client, receipt_boundary, monkeypatch):
    requests = _model_script(monkeypatch, [_reply(text="你好。")])
    run = _run(client, receipt_boundary, streaming=False, enabled_tools=["update_plan"])
    assert run["task_plan"] is None and len(requests) == 1
    assert requests[0]["messages"][-1]["role"] == "user"
    assert "Skip it for greetings" in requests[0]["messages"][1]["content"]


@pytest.mark.parametrize("state", [None, {}, {"version": True, "model_calls": 0, "plan_attempts": 0},
                                   {"version": 1, "model_calls": 16, "plan_attempts": 0},
                                   {"version": 1, "model_calls": "1", "plan_attempts": 0},
                                   {"version": 1, "model_calls": 1, "plan_attempts": 9}])
def test_invalid_loop_controls_never_grant_new_quota(state):
    with pytest.raises(ValueError):
        RunLoopControl.from_state(state)


def test_run_control_roundtrip_and_argument_bounds_are_independent_of_receipts():
    control = RunLoopControl(model_calls=4)
    assert all(control.attempt_plan() for _ in range(8))
    assert control.attempt_plan() is False
    restored = RunLoopControl.from_state(json.loads(json.dumps(control.to_state())))
    assert restored.model_calls == 4 and restored.plan_attempts == 8
    assert restored.attempt_plan() is False
    assert PlanUpdateArguments.model_validate({"plan": []}).public_steps() == []
    with pytest.raises(ValidationError):
        PlanUpdateArguments.model_validate({"plan": [{"step": "x" * 161, "status": "pending"}]})
