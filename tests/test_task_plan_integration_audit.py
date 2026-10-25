"""Independent HTTP/DB audit of public plans, separate from action evidence.

Only model replies and the existing read-only tool boundary are faked. Auth,
managed workers, durable events, approval writes and plan persistence stay real.
"""

from __future__ import annotations

import json
import threading

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.services.llm_service import LLMService, LLMStreamChunk
from tests.auth_helpers import login_existing_user, register_workspace_user_in_team
from tests.test_execution_receipt import (
    _approve,
    _call,
    _memory_count,
    _model_script,
    _receipt,
    _reply,
    _run,
    receipt_boundary,
)


def _plan(content="检查交付物", status="pending", *, identifier="public-plan", **extra):
    return _call("update_plan", {
        "plan": [{"step": content, "status": status}], **extra,
    }, identifier=identifier)


def _write(number):
    return _call("create_memory_card", {
        "title": f"Plan audit {number}", "content": f"Approved isolated write {number}",
    }, identifier=f"approved-write-{number}")


def _events(client, run_id):
    response = client.get(f"/api/v1/agent/runs/{run_id}/events?limit=500")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _checkpoint(run_id):
    with SessionLocal() as db:
        return json.loads(db.get(AgentRun, run_id).loop_state_json)


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
@pytest.mark.parametrize("mixed", [False, True], ids=["failed", "partial"])
def test_model_checkmarks_never_override_observed_external_failure(
    client, receipt_boundary, monkeypatch, streaming, mixed,
):
    actions = [_call(arguments={"limit": 2}, identifier="actually-failed")]
    if mixed:
        actions.insert(0, _call(identifier="actually-returned"))
    _model_script(monkeypatch, [
        _reply(_plan(status="in_progress"), *actions),
        _reply(_plan(status="completed", identifier="claimed-complete")),
        _reply(text="模型声称所有步骤完成。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming,
               enabled_tools=["update_plan", "list_recent_documents"])
    assert run["task_plan"]["model_reported"] is True
    assert run["task_plan"]["revision"] == 2
    assert run["task_plan"]["steps"][0]["status"] == "completed"
    assert run["status"] == ("partial_success" if mixed else "failed"), run
    receipt = _receipt(run)
    assert receipt["tool_calls_failed"] == 1 and receipt["incomplete"] is True
    assert receipt["tool_calls_succeeded"] == int(mixed)
    assert receipt["tool_calls_used"] == len(actions)
    assert receipt["tool_calls_rejected"] == 0
    assert all(call["action"] != "update_plan" for call in receipt_boundary["attempted"])


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_plan_can_update_after_external_budget_without_spending_an_action(
    client, receipt_boundary, monkeypatch, streaming,
):
    requests = _model_script(monkeypatch, [
        _reply(_plan(status="in_progress"), *[
            _call(arguments={"limit": number}, identifier=f"read-{number}") for number in (1, 3, 4)
        ]),
        _reply(_plan(status="completed", identifier="record-completion")),
        _reply(text="三次查询均已返回。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming, max_steps=3,
               enabled_tools=["update_plan", "list_recent_documents"])
    assert run["status"] == "completed", run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 3
    assert receipt["tool_calls_rejected"] == receipt["omitted_tool_calls"] == 0
    assert receipt["incomplete"] is False and receipt["stop_reason"] == "tool_budget"
    assert run["task_plan"]["revision"] == 2
    assert [tool["function"]["name"] for tool in requests[1]["tools"]] == ["update_plan"]
    assert len(receipt_boundary["attempted"]) == 3


def test_valid_duplicate_and_invalid_plan_attempts_share_one_budget_across_three_approvals(
    client, receipt_boundary, monkeypatch,
):
    replies = []
    for number in range(1, 4):
        plans = [
            _plan(f"第 {number} 项", "in_progress", identifier=f"plan-{number}"),
            _plan(f"第 {number} 项", "in_progress", identifier=f"duplicate-{number}"),
        ]
        if number < 3:
            plans.append(_plan("非法计划", "unknown", identifier=f"invalid-{number}"))
        replies.append(_reply(*plans, _write(number)))
    replies.append(_reply(
        _plan("预算外计划不应更新", "completed", identifier="ninth-plan"),
        _write(4), text="仍有一项没有执行。",
    ))
    requests = _model_script(monkeypatch, replies)
    run = _run(client, receipt_boundary, streaming=True, max_steps=3,
               enabled_tools=["update_plan", "create_memory_card"])
    for number, attempts in enumerate((3, 6, 8), 1):
        assert run["status"] == "requires_confirmation", run
        state = _checkpoint(run["run_id"])
        assert state["version"] == 2
        assert state["loop_control"]["plan_attempts"] == attempts
        assert state["loop_control"]["model_calls"] == number
        assert run["task_plan"]["revision"] == number
        assert _receipt(run)["tool_calls_used"] == number
        response = _approve(client, run)
        assert response.status_code == 200, response.text
        run = response.json()
        assert _memory_count(receipt_boundary) == number
    assert run["status"] == "partial_success", run
    assert run["task_plan"]["revision"] == 3
    assert run["task_plan"]["steps"][0]["status"] == "in_progress"
    metadata = [step for step in run["steps"] if step["step_type"] == "task_plan"]
    assert len(metadata) == 8
    assert sum(step["status"] == "failed" for step in metadata) == 2
    assert _receipt(run)["tool_calls_rejected"] == 0
    assert _receipt(run)["tool_calls_succeeded"] == 3
    assert _receipt(run)["omitted_tool_calls"] == 1
    assert len(requests) == 4 and requests[-1]["tools"] is None


def test_fifteen_model_requests_are_shared_by_approval_continuations_including_final_summary(
    client, receipt_boundary, monkeypatch,
):
    replies = [_reply(_plan(f"计划 {number}", identifier=f"plan-{number}")) for number in range(1, 8)]
    replies.extend([
        _reply(_write(1)),
        _reply(_plan("计划 8", identifier="plan-8")),
        _reply(_write(2)),
        *[_reply(_plan("预算外计划", identifier=f"ignored-{number}")) for number in range(11, 15)],
        _reply(_write(3), text="最后一个模型请求仍不能执行第三次写入。"),
    ])
    requests = _model_script(monkeypatch, replies)
    run = _run(client, receipt_boundary, streaming=True, max_steps=10,
               enabled_tools=["update_plan", "create_memory_card"])
    for expected_model_calls, expected_plans in ((8, 7), (10, 8)):
        assert run["status"] == "requires_confirmation", run
        state = _checkpoint(run["run_id"])["loop_control"]
        assert state["model_calls"] == expected_model_calls
        assert state["plan_attempts"] == expected_plans
        response = _approve(client, run)
        assert response.status_code == 200, response.text
        run = response.json()
    assert run["status"] == "partial_success", run
    assert len(requests) == 15 and requests[-1]["tools"] is None
    assert run["task_plan"]["revision"] == 8
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 2
    assert receipt["omitted_tool_calls"] == 1 and receipt["stop_reason"] == "iteration_limit"
    assert _memory_count(receipt_boundary) == 2


def test_over_limit_plan_proposal_does_not_poison_a_later_valid_approval(
    client, receipt_boundary, monkeypatch,
):
    requests = _model_script(monkeypatch, [
        _reply(*[_plan(f"计划 {number}", identifier=f"plan-{number}") for number in range(1, 10)], _write(1)),
        _reply(text="本轮授权的操作已经返回。"),
    ])
    pending = _run(client, receipt_boundary, streaming=True,
                   enabled_tools=["update_plan", "create_memory_card"])
    assert pending["status"] == "requires_confirmation", pending
    assert pending["task_plan"]["revision"] == 8
    assert _checkpoint(pending["run_id"])["loop_control"]["plan_attempts"] == 8
    response = _approve(client, pending)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "completed", run
    assert _receipt(run)["tool_calls_succeeded"] == 1
    assert _memory_count(receipt_boundary) == 1 and len(requests) == 2


@pytest.mark.parametrize("damage", [
    "deleted", "invalid_type", "over_limit", "understated_plans", "understated_model_calls", "version_removed",
    "version_only_removed", "missing_batch", "noninteger_batch", "backward_batch", "counter_batch_mismatch",
])
def test_damaged_current_loop_control_cannot_dispatch_an_approved_write(
    client, receipt_boundary, monkeypatch, damage,
):
    requests = _model_script(monkeypatch, [
        _reply(_plan("第一步", identifier="plan-one")),
        _reply(_plan("第二步", identifier="plan-two")),
        _reply(_write(1)),
        _reply(text="没有可信控制记录不能继续。"),
    ])
    pending = _run(client, receipt_boundary, streaming=True,
                   enabled_tools=["update_plan", "create_memory_card"])
    assert pending["status"] == "requires_confirmation"
    with SessionLocal() as db:
        record = db.get(AgentRun, pending["run_id"])
        state = json.loads(record.loop_state_json)
        if damage == "deleted":
            state.pop("loop_control")
        elif damage == "invalid_type":
            state["loop_control"]["model_calls"] = True
        elif damage == "over_limit":
            state["loop_control"]["model_calls"] = 16
        elif damage == "understated_plans":
            state["loop_control"]["plan_attempts"] = 1
        elif damage == "understated_model_calls":
            state["loop_control"]["model_calls"] = 1
        elif damage == "version_removed":
            state.pop("version")
            state.pop("loop_control")
        elif damage == "version_only_removed":
            state.pop("version")
        else:
            assistants = [message for message in state["messages"]
                          if message.get("role") == "assistant" and message.get("tool_calls")]
            if damage == "missing_batch":
                assistants[0].pop("_caibao_model_call")
            elif damage == "noninteger_batch":
                assistants[0]["_caibao_model_call"] = True
            elif damage == "backward_batch":
                assistants[0]["_caibao_model_call"], assistants[1]["_caibao_model_call"] = 2, 1
            else:
                assistants[-1]["_caibao_model_call"] = 4
        record.loop_state_json = json.dumps(state)
        db.commit()
    # Fetch the current token: the rejection must be about evidence, rather
    # than an intentionally stale token whose digest included the old state.
    damaged = client.get(f"/api/v1/agent/runs/{pending['run_id']}").json()
    before = len(requests)
    for _ in range(2):
        response = _approve(client, damaged)
        assert response.status_code == 400, response.text
        current = client.get(f"/api/v1/agent/runs/{pending['run_id']}").json()
        assert current["status"] == "requires_confirmation"
        assert current["task_plan"] == pending["task_plan"]
    assert len(requests) == before
    assert receipt_boundary["attempted"] == [] and _memory_count(receipt_boundary) == 0


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_parallel_batches_are_one_model_request_and_private_counters_never_reach_provider(
    client, receipt_boundary, monkeypatch, streaming,
):
    requests = _model_script(monkeypatch, [
        _reply(_plan("同一批计划一", identifier="plan-one"), _call(identifier="read-one"),
               _plan("同一批计划二", identifier="plan-two"),
               _call(arguments={"limit": 3}, identifier="read-two"), _write(1)),
        _reply(_plan("第二批计划", identifier="plan-three"), _write(2)),
        _reply(text="两次单独授权的写入已返回。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming, max_steps=5,
               enabled_tools=["update_plan", "list_recent_documents", "create_memory_card"])
    for number, expected_batches in ((1, [1, 1, 1, 1, 1]), (2, [1, 1, 1, 1, 1, 2, 2])):
        assert run["status"] == "requires_confirmation", run
        state = _checkpoint(run["run_id"])
        batches = [message.get("_caibao_model_call") for message in state["messages"]
                   if message.get("role") == "assistant" and message.get("tool_calls")]
        assert batches == expected_batches
        assert state["loop_control"]["model_calls"] == number
        response = _approve(client, run)
        assert response.status_code == 200, response.text
        run = response.json()
    assert run["status"] == "completed", run
    assert run["task_plan"]["revision"] == 3
    assert _receipt(run)["tool_calls_succeeded"] == 4
    assert _memory_count(receipt_boundary) == 2 and len(requests) == 3
    assert "_caibao_" not in json.dumps(requests), "private batch fields must be stripped recursively before provider calls"


def test_legacy_parallel_transcript_gets_conservative_batches_and_survives_second_approval(
    client, receipt_boundary, monkeypatch,
):
    requests = _model_script(monkeypatch, [
        _reply(_call(identifier="legacy-read"), _write(1)),
        _reply(_write(2)),
        _reply(text="两次审批均按原有预算继续。"),
    ])
    run = _run(client, receipt_boundary, streaming=True, max_steps=5,
               enabled_tools=["list_recent_documents", "create_memory_card"])
    assert run["status"] == "requires_confirmation"
    with SessionLocal() as db:
        record = db.get(AgentRun, run["run_id"])
        state = json.loads(record.loop_state_json)
        for key in ("version", "loop_control", "execution_ledger"):
            state.pop(key)
        for message in state["messages"]:
            message.pop("_caibao_model_call", None)
            for call in message.get("tool_calls") or []:
                call.pop("_caibao_model_call", None)
        record.loop_state_json = json.dumps(state)
        record.execution_receipt_json = None
        db.commit()
    legacy = client.get(f"/api/v1/agent/runs/{run['run_id']}").json()
    first = _approve(client, legacy)
    assert first.status_code == 200, first.text
    next_pending = first.json()
    assert next_pending["status"] == "requires_confirmation"
    upgraded = _checkpoint(run["run_id"])
    assert upgraded["version"] == 2 and upgraded["loop_control"]["model_calls"] == 3
    assert [message.get("_caibao_model_call") for message in upgraded["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")] == [1, 2, 3]
    second = _approve(client, next_pending)
    assert second.status_code == 200, second.text
    completed = second.json()
    assert completed["status"] == "completed", completed
    assert _receipt(completed)["tool_calls_used"] == _receipt(completed)["tool_calls_succeeded"] == 3
    assert _memory_count(receipt_boundary) == 2 and len(requests) == 3
    assert "_caibao_" not in json.dumps(requests)


def test_cancel_while_waiting_for_model_ignores_late_plan_and_preserves_previous_snapshot(
    client, receipt_boundary, monkeypatch,
):
    entered, release, model_closed = threading.Event(), threading.Event(), threading.Event()
    model_calls = []

    def model(self, **kwargs):
        model_calls.append(kwargs)
        if len(model_calls) == 1:
            yield LLMStreamChunk(tool_call_deltas=[{"index": 0, **_plan("原有未完成项", "in_progress")}])
            return
        entered.set()
        try:
            assert release.wait(5), "test did not release the fake model"
            yield LLMStreamChunk(tool_call_deltas=[{"index": 0, **_plan("迟到勾选", "completed")}])
        finally:
            model_closed.set()

    monkeypatch.setattr(LLMService, "stream_chat", model)
    admitted = client.post("/api/v1/agent/runs", json={
        "task": "追踪多步骤工作", "conversation_id": receipt_boundary["conversation"]["conversation_id"],
        "trigger_channel": "companion", "include_memory": False, "include_library": False,
        "enabled_tools": ["update_plan"],
    })
    assert admitted.status_code == 200, admitted.text
    run_id = admitted.json()["run_id"]
    try:
        assert client.post(f"/api/v1/agent/runs/{run_id}/start").status_code == 200
        assert entered.wait(5), "fake model never reached the in-flight boundary"
        prior = client.get(f"/api/v1/agent/runs/{run_id}").json()["task_plan"]
        assert prior["revision"] == 1
        cancelled = client.post(f"/api/v1/agent/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
    finally:
        release.set()
    assert model_closed.wait(5), "cancelled worker did not close its fake model iterator"
    run = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert run["status"] == "cancelled" and run["task_plan"] == prior
    revisions = [event["payload"]["task_plan"]["revision"] for event in _events(client, run_id)
                 if event["event"] == "plan.updated"]
    assert revisions == [1]
    assert receipt_boundary["attempted"] == []


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "interrupted"])
def test_late_model_plan_cannot_mutate_an_already_terminal_snapshot(
    client, receipt_boundary, monkeypatch, terminal_status,
):
    model_calls = []

    def model(self, **kwargs):
        model_calls.append(kwargs)
        if len(model_calls) == 1:
            return _reply(_plan("终态前的快照", "in_progress"))
        if len(model_calls) == 2:
            with SessionLocal() as db:
                record = db.scalar(select(AgentRun).where(
                    AgentRun.user_id == receipt_boundary["user"], AgentRun.status == "running",
                ))
                assert record is not None
                record.status = terminal_status
                db.commit()
            return _reply(_plan("终态后迟到勾选", "completed"))
        return _reply(text="保持先前计划记录。")

    monkeypatch.setattr(LLMService, "complete_chat", model)
    run = _run(client, receipt_boundary, streaming=False, enabled_tools=["update_plan"])
    assert run["task_plan"]["revision"] == 1
    assert run["task_plan"]["steps"][0]["content"] == "终态前的快照"
    assert run["task_plan"]["steps"][0]["status"] == "in_progress"
    assert receipt_boundary["attempted"] == []


def test_plan_history_replay_owner_isolation_and_new_run_do_not_reexecute_or_inherit(
    client, receipt_boundary, monkeypatch,
):
    requests = _model_script(monkeypatch, [
        _reply(_plan("仅本人的计划", "in_progress"), _call()),
        _reply(_plan("仅本人的计划", "completed", identifier="final-plan")),
        _reply(text="查询已经返回。"),
        _reply(text="新任务直接回答，不生成计划。"),
    ])
    run = _run(client, receipt_boundary, streaming=True,
               enabled_tools=["update_plan", "list_recent_documents"])
    plan, run_id = run["task_plan"], run["run_id"]
    before = len(requests)
    events = _events(client, run_id)
    plans = [event for event in events if event["event"] == "plan.updated"]
    assert [event["payload"]["task_plan"]["revision"] for event in plans] == [1, 2]
    for _ in range(2):
        response = client.get(f"/api/v1/agent/runs/{run_id}/stream")
        assert response.status_code == 200
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert [frame["payload"]["task_plan"] for frame in frames if frame["event"] == "plan.updated"] == [
            event["payload"]["task_plan"] for event in plans
        ]
        assert client.post(f"/api/v1/agent/runs/{run_id}/start").status_code == 200
    history = client.get("/api/v1/chat/history", params={
        "conversation_id": receipt_boundary["conversation"]["conversation_id"],
    }).json()["items"]
    recorded = next(item["response_payload"] for item in history if item["response_payload"].get("run_id") == run_id)
    assert recorded["task_plan"] == plan
    assert recorded["execution_receipt"] == run["execution_receipt"]
    assert len(requests) == before and len(receipt_boundary["attempted"]) == 1

    next_run = _run(client, receipt_boundary, streaming=True, task="只回答这个新任务。",
                    enabled_tools=["update_plan", "list_recent_documents"])
    assert next_run["run_id"] != run_id and next_run["task_plan"] is None
    assert not any(event["event"] == "plan.updated" for event in _events(client, next_run["run_id"]))
    assert client.get(f"/api/v1/agent/runs/{run_id}").json()["task_plan"] == plan

    register_workspace_user_in_team(client, team_id=receipt_boundary["team"],
                                    prefix="plan_audit_other", display_name="Other member")
    for suffix in ("", "/events", "/stream"):
        response = client.get(f"/api/v1/agent/runs/{run_id}{suffix}")
        assert response.status_code == 404 and "仅本人的计划" not in response.text
    assert client.post(f"/api/v1/agent/runs/{run_id}/start").status_code == 404
    assert client.get("/api/v1/agent/runs").json()["items"] == []
    other_history = client.get("/api/v1/chat/history", params={
        "conversation_id": receipt_boundary["conversation"]["conversation_id"],
    })
    assert "仅本人的计划" not in other_history.text
    login_existing_user(client, user_id=receipt_boundary["user"])
    assert client.get(f"/api/v1/agent/runs/{run_id}").json()["task_plan"] == plan
    assert len(requests) == before + 1 and len(receipt_boundary["attempted"]) == 1
