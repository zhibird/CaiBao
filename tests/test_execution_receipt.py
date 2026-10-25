"""Independent HTTP regressions for truthful execution outcomes and run budgets.

The test suite provides an isolated temporary database. Only model replies and
the selected read-only document tool are faked; authentication, admission,
background ownership, stream parsing, approvals and memory writes stay real.
"""

from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy import select

from app.core.exceptions import DomainValidationError
from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.memory_card import MemoryCard
from app.services.llm_service import LLMCompletionResult, LLMService, LLMStreamChunk
from app.services.tool_service import ToolService
from tests.auth_helpers import register_workspace_user


def _call(name="list_recent_documents", arguments=None, *, identifier="call-1"):
    arguments = {"limit": 1} if arguments is None else arguments
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
    }}


def _reply(*calls, text=""):
    return LLMCompletionResult(assistant_text=text, tool_calls=list(calls))


def _model_script(monkeypatch, replies):
    remaining = list(replies)
    requests = []

    def next_reply(kind, kwargs):
        requests.append({"kind": kind, **copy.deepcopy(kwargs)})
        result = remaining.pop(0) if remaining else _reply(text="已返回本轮结果。")
        if isinstance(result, Exception):
            raise result
        return result

    def complete(self, **kwargs):
        return next_reply("sync", kwargs)

    def stream(self, **kwargs):
        result = next_reply("stream", kwargs)
        if result.assistant_text:
            yield LLMStreamChunk(delta_text=result.assistant_text)
        for index, call in enumerate(result.tool_calls):
            yield LLMStreamChunk(tool_call_deltas=[{"index": index, **call}])

    monkeypatch.setattr(LLMService, "complete_chat", complete)
    monkeypatch.setattr(LLMService, "stream_chat", stream)
    return requests


@pytest.fixture
def receipt_boundary(client, monkeypatch):
    team, user = register_workspace_user(client, prefix="execution_receipt", display_name="Receipt owner")
    conversation = client.post("/api/v1/conversations", json={"title": "Execution evidence"})
    assert conversation.status_code == 201, conversation.text
    original_execute = ToolService.execute
    attempted = []

    def execute(self, **kwargs):
        attempted.append(copy.deepcopy(kwargs))
        if kwargs["action"] == "list_recent_documents":
            if kwargs["arguments"].get("limit") == 2:
                raise RuntimeError("LOCAL_RECEIPT_DOCUMENT_TOOL_FAILED")
            return {"documents": [{"document_id": "local-result", "title": "Verified tool return"}]}
        return original_execute(self, **kwargs)

    monkeypatch.setattr(ToolService, "execute", execute)
    return {"team": team, "user": user, "conversation": conversation.json(), "attempted": attempted}


def _run(client, boundary, *, streaming, **changes):
    payload = {
        "task": "完成这个多步骤测试任务，按实际结果告诉我。",
        "conversation_id": boundary["conversation"]["conversation_id"],
        "trigger_channel": "companion", "include_memory": False, "include_library": False,
        "enabled_tools": ["list_recent_documents"], "max_steps": 5, **changes,
    }
    if not streaming:
        response = client.post("/api/v1/agent/run", json=payload)
    else:
        queued = client.post("/api/v1/agent/runs", json=payload)
        assert queued.status_code == 200, queued.text
        run_id = queued.json()["run_id"]
        observed = client.get(f"/api/v1/agent/runs/{run_id}/stream")
        assert observed.status_code == 200, observed.text
        response = client.get(f"/api/v1/agent/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _receipt(run):
    receipt = run.get("execution_receipt")
    assert isinstance(receipt, dict), "a managed API run must publish its own execution receipt"
    assert receipt["version"] == 1 and receipt["source"] == "api_tool_loop"
    assert receipt["tool_calls_used"] == sum(receipt[key] for key in (
        "tool_calls_succeeded", "tool_calls_failed", "tool_calls_rejected", "tool_calls_skipped", "pending_tool_calls",
    ))
    assert receipt["tool_calls_used"] <= receipt["tool_budget"]
    return receipt


def _approve(client, run):
    return client.post(f"/api/v1/agent/runs/{run['run_id']}/confirm", json={"approval_token": run["approval_token"]})


def _memory_count(boundary):
    with SessionLocal() as db:
        return len(list(db.scalars(select(MemoryCard).where(
            MemoryCard.team_id == boundary["team"], MemoryCard.user_id == boundary["user"],
        ))))


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_failed_tool_cannot_be_reported_as_successful_execution(client, receipt_boundary, monkeypatch, streaming):
    _model_script(monkeypatch, [
        _reply(_call(arguments={"limit": 2})),
        _reply(text="工具失败了，无法完成任务。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming)
    assert run["status"] == "failed", run
    assert [step["status"] for step in run["steps"] if step["step_type"] == "tool_call"] == ["failed"]
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_failed"] == 1
    assert receipt["tool_calls_succeeded"] == 0 and receipt["incomplete"] is True
    assert "LOCAL_RECEIPT_DOCUMENT_TOOL_FAILED" not in json.dumps(receipt)


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_mixed_tool_outcomes_publish_partial_execution(client, receipt_boundary, monkeypatch, streaming):
    _model_script(monkeypatch, [
        _reply(_call(identifier="read-1"), _call(arguments={"limit": 2}, identifier="read-2")),
        _reply(text="只能完成第一部分。"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming)
    assert run["status"] == "partial_success", run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == 2 and receipt["tool_calls_succeeded"] == receipt["tool_calls_failed"] == 1
    assert receipt["incomplete"] is True
    assert len(receipt_boundary["attempted"]) == 2


def test_approval_continuations_share_one_budget_and_cannot_execute_a_fourth_write(client, receipt_boundary, monkeypatch):
    replies = [_reply(_call("create_memory_card", {"title": f"Budget {index}", "content": f"Isolated memory {index}"}, identifier=f"write-{index}")) for index in range(1, 5)]
    _model_script(monkeypatch, [*replies, _reply(text="仍有一条没有保存。")])
    run = _run(client, receipt_boundary, streaming=True, enabled_tools=["create_memory_card"], max_steps=3)
    for expected_writes in range(1, 4):
        assert run["status"] == "requires_confirmation", run
        waiting = _receipt(run)
        assert waiting["tool_calls_used"] == expected_writes
        assert waiting["pending_tool_calls"] == 1 and waiting["tool_calls_succeeded"] == expected_writes - 1
        response = _approve(client, run)
        assert response.status_code == 200, response.text
        run = response.json()
        assert _memory_count(receipt_boundary) == expected_writes
    assert run["status"] == "partial_success", run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 3
    assert receipt["pending_tool_calls"] == 0
    assert receipt["omitted_tool_calls"] >= 1 and receipt["stop_reason"] == "tool_budget"
    assert receipt["incomplete"] is True
    assert len(receipt_boundary["attempted"]) == 3


def test_repeated_approval_does_not_consume_budget_or_write_twice(client, receipt_boundary, monkeypatch):
    _model_script(monkeypatch, [
        _reply(_call("create_memory_card", {"title": "Once", "content": "Write once"})),
        _reply(text="已经返回。"),
    ])
    pending = _run(client, receipt_boundary, streaming=True, enabled_tools=["create_memory_card"], max_steps=3)
    waiting = _receipt(pending)
    assert waiting["tool_calls_used"] == waiting["pending_tool_calls"] == 1
    approved = _approve(client, pending)
    assert approved.status_code == 200, approved.text
    completed = approved.json()
    assert completed["status"] == "completed"
    receipt = _receipt(completed)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 1
    assert receipt["pending_tool_calls"] == 0
    assert _approve(client, pending).status_code == 400
    repeated = client.get(f"/api/v1/agent/runs/{pending['run_id']}").json()
    assert repeated["execution_receipt"] == receipt
    assert _memory_count(receipt_boundary) == 1


def test_prior_failure_does_not_disappear_after_an_approval_continuation(client, receipt_boundary, monkeypatch):
    _model_script(monkeypatch, [
        _reply(_call(arguments={"limit": 2}, identifier="failed-read"), _call("create_memory_card", {"title": "Partial", "content": "Only this part succeeded"}, identifier="pending-write")),
        _reply(text="保存了记忆，但读取部分仍失败。"),
    ])
    pending = _run(client, receipt_boundary, streaming=True, enabled_tools=["list_recent_documents", "create_memory_card"])
    assert pending["status"] == "requires_confirmation"
    receipt = _receipt(pending)
    assert receipt["tool_calls_failed"] == receipt["pending_tool_calls"] == 1
    approved = _approve(client, pending)
    assert approved.status_code == 200, approved.text
    run = approved.json()
    assert run["status"] == "partial_success", run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == 2 and receipt["tool_calls_failed"] == receipt["tool_calls_succeeded"] == 1
    assert receipt["incomplete"] is True and _memory_count(receipt_boundary) == 1


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
@pytest.mark.parametrize("violation", ["disabled", "unknown", "invalid_json"])
def test_rejected_tool_proposals_cannot_execute_or_become_success(client, receipt_boundary, monkeypatch, streaming, violation):
    proposal = _call()
    enabled = ["list_recent_documents"]
    if violation == "disabled":
        enabled = []
    elif violation == "unknown":
        proposal = _call("not_a_real_tool")
    else:
        proposal = _call(arguments='{not valid JSON')
    _model_script(monkeypatch, [_reply(proposal), _reply(text="没有取得可用的工具结果。")])
    run = _run(client, receipt_boundary, streaming=streaming, enabled_tools=enabled)
    assert run["status"] == "failed", run
    receipt = _receipt(run)
    assert receipt["tool_calls_rejected"] == receipt["tool_calls_used"] == 1
    assert receipt["tool_calls_succeeded"] == 0 and receipt["incomplete"] is True
    assert receipt_boundary["attempted"] == []


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_exact_successful_budget_can_complete_without_inventing_unfinished_work(client, receipt_boundary, monkeypatch, streaming):
    calls = [_call(arguments={"limit": number}, identifier=f"read-{number}") for number in (1, 3, 4)]
    requests = _model_script(monkeypatch, [_reply(*calls), _reply(text="三次查询都返回了。")])
    run = _run(client, receipt_boundary, streaming=streaming, max_steps=3)
    assert run["status"] == "completed", run
    receipt = _receipt(run)
    assert receipt["tool_calls_succeeded"] == receipt["tool_calls_used"] == 3
    assert receipt["omitted_tool_calls"] == 0 and receipt["incomplete"] is False
    assert len(receipt_boundary["attempted"]) == 3
    assert requests[-1]["tools"] is None


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_a_fourth_proposal_in_one_batch_is_reported_as_omitted_work(client, receipt_boundary, monkeypatch, streaming):
    calls = [_call(arguments={"limit": number}, identifier=f"read-{number}") for number in (1, 3, 4, 5)]
    _model_script(monkeypatch, [_reply(*calls), _reply(text="预算用尽，最后一项没有查询。")])
    run = _run(client, receipt_boundary, streaming=streaming, max_steps=3)
    assert run["status"] == "partial_success", run
    receipt = _receipt(run)
    assert receipt["tool_calls_succeeded"] == receipt["tool_calls_used"] == 3
    assert receipt["omitted_tool_calls"] >= 1 and receipt["incomplete"] is True
    assert receipt["stop_reason"] == "tool_budget"
    assert len(receipt_boundary["attempted"]) == 3


def test_duplicate_guard_survives_approval_boundaries(client, receipt_boundary, monkeypatch):
    repeated = {"title": "Repeated operation", "content": "Same exact operation each round"}
    replies = [_reply(_call("create_memory_card", repeated, identifier=f"same-{number}")) for number in range(4)]
    _model_script(monkeypatch, replies)
    run = _run(client, receipt_boundary, streaming=True, enabled_tools=["create_memory_card"], max_steps=10)
    # CaiBao's existing duplicate boundary is two consecutive equal calls. An
    # approval only completes a reserved call; it must not erase that history.
    for _ in range(2):
        assert run["status"] == "requires_confirmation", run
        approved = _approve(client, run)
        assert approved.status_code == 200, approved.text
        run = approved.json()
    assert run["status"] == "partial_success", run
    receipt = _receipt(run)
    assert receipt["stop_reason"] == "duplicate_guard" and receipt["incomplete"] is True
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 2
    assert receipt["pending_tool_calls"] == 0 and len(receipt_boundary["attempted"]) == 2


def test_reading_and_reobserving_a_run_preserves_its_receipt_without_execution(client, receipt_boundary, monkeypatch):
    requests = _model_script(monkeypatch, [_reply(_call()), _reply(text="读取已返回。")])
    run = _run(client, receipt_boundary, streaming=True)
    receipt = _receipt(run)
    count = len(requests)
    run_id = run["run_id"]
    for _ in range(2):
        observed = client.get(f"/api/v1/agent/runs/{run_id}/stream")
        assert observed.status_code == 200, observed.text
        assert client.post(f"/api/v1/agent/runs/{run_id}/start").status_code == 200
        current = client.get(f"/api/v1/agent/runs/{run_id}").json()
        assert current["execution_receipt"] == receipt
    history = client.get("/api/v1/chat/history", params={"conversation_id": receipt_boundary["conversation"]["conversation_id"]}).json()["items"]
    recorded = next(item["response_payload"] for item in history if item["response_payload"].get("run_id") == run_id)
    assert recorded["execution_receipt"] == receipt
    assert len(requests) == count and len(receipt_boundary["attempted"]) == 1


def test_cancelling_a_reserved_approval_preserves_accounting_and_never_writes(client, receipt_boundary, monkeypatch):
    _model_script(monkeypatch, [_reply(_call("create_memory_card", {"title": "Never", "content": "Cancelled before dispatch"}))])
    pending = _run(client, receipt_boundary, streaming=True, enabled_tools=["create_memory_card"])
    waiting = _receipt(pending)
    assert waiting["tool_calls_used"] == waiting["pending_tool_calls"] == 1
    cancelled = client.post(f"/api/v1/agent/runs/{pending['run_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    run = cancelled.json()
    assert run["status"] == "cancelled"
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == 1 and receipt["stop_reason"] == "cancelled"
    assert receipt["incomplete"] is True
    assert _memory_count(receipt_boundary) == 0 and receipt_boundary["attempted"] == []
    replay = client.get(f"/api/v1/agent/runs/{run['run_id']}/stream")
    assert replay.status_code == 200
    assert client.get(f"/api/v1/agent/runs/{run['run_id']}").json()["execution_receipt"] == receipt
    assert _approve(client, pending).status_code == 400
    assert receipt_boundary["attempted"] == []


def test_legacy_approval_reconstructs_consumed_budget_from_real_tool_messages(client, receipt_boundary, monkeypatch):
    requests = _model_script(monkeypatch, [
        _reply(_call(identifier="legacy-read")),
        _reply(_call("create_memory_card", {"title": "Legacy pending", "content": "Original pending write"}, identifier="legacy-write")),
        _reply(_call(arguments={"limit": 3}, identifier="last-budget-read")),
        _reply(text="原预算内的三次动作都已返回。"),
    ])
    pending = _run(client, receipt_boundary, streaming=True, enabled_tools=["list_recent_documents", "create_memory_card"], max_steps=3)
    assert pending["status"] == "requires_confirmation"
    assert _receipt(pending)["tool_calls_used"] == 2
    # Reproduce a pre-ledger record while preserving the genuine assistant/tool
    # transcript written by the first execution, rather than inventing a result.
    with SessionLocal() as db:
        record = db.get(AgentRun, pending["run_id"])
        state = json.loads(record.loop_state_json)
        state.pop("execution_ledger")
        state.pop("version")
        state.pop("loop_control")
        for message in state["messages"]:
            message.pop("_caibao_model_call", None)
        record.loop_state_json = json.dumps(state)
        record.execution_receipt_json = None
        db.commit()
    legacy = client.get(f"/api/v1/agent/runs/{pending['run_id']}").json()
    assert legacy["execution_receipt"] is None
    approved = _approve(client, legacy)
    assert approved.status_code == 200, approved.text
    run = approved.json()
    assert run["status"] == "completed", run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 3
    assert receipt["omitted_tool_calls"] == 0 and receipt["incomplete"] is False
    assert requests[-1]["tools"] is None
    assert len(receipt_boundary["attempted"]) == 3 and _memory_count(receipt_boundary) == 1


@pytest.mark.parametrize("damage", [
    "inconsistent_ledger", "unverifiable_legacy_result", "missing_checkpoint", "missing_legacy_checkpoint",
])
def test_corrupt_or_unverifiable_approval_evidence_cannot_grant_a_fresh_budget(client, receipt_boundary, monkeypatch, damage):
    requests = _model_script(monkeypatch, [
        _reply(_call(identifier="trusted-read")),
        _reply(_call("create_memory_card", {"title": "Must wait", "content": "No evidence, no dispatch"}, identifier="unexecuted-write")),
        _reply(text="不能在损坏的预算证据下继续。"),
    ])
    pending = _run(client, receipt_boundary, streaming=True, enabled_tools=["list_recent_documents", "create_memory_card"], max_steps=3)
    assert pending["status"] == "requires_confirmation"
    with SessionLocal() as db:
        record = db.get(AgentRun, pending["run_id"])
        state = json.loads(record.loop_state_json)
        if damage in {"missing_checkpoint", "missing_legacy_checkpoint"}:
            record.loop_state_json = None
            if damage == "missing_legacy_checkpoint":
                record.execution_receipt_json = None
        elif damage == "inconsistent_ledger":
            state["execution_ledger"]["used"] = 0
        else:
            state.pop("execution_ledger")
            state.pop("version")
            state.pop("loop_control")
            for message in state["messages"]:
                message.pop("_caibao_model_call", None)
            next(message for message in state["messages"] if message["role"] == "tool")["content"] = "{}"
        if damage not in {"missing_checkpoint", "missing_legacy_checkpoint"}:
            record.loop_state_json = json.dumps(state)
        db.commit()
    damaged = client.get(f"/api/v1/agent/runs/{pending['run_id']}").json()
    before = len(requests)
    for _ in range(2):
        rejected = _approve(client, damaged)
        assert rejected.status_code == 400, rejected.text
        current = client.get(f"/api/v1/agent/runs/{pending['run_id']}").json()
        assert current["status"] == "requires_confirmation"
        assert current["required_confirmations"] == damaged["required_confirmations"]
        assert current["approval_token"] == damaged["approval_token"]
    assert len(requests) == before and len(receipt_boundary["attempted"]) == 1
    assert _memory_count(receipt_boundary) == 0


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_provider_tool_unsupported_after_execution_cannot_fallback_and_repeat_work(client, receipt_boundary, monkeypatch, streaming):
    requests = _model_script(monkeypatch, [
        _reply(_call()), DomainValidationError("LLM_TOOLS_UNSUPPORTED"),
    ])
    run = _run(client, receipt_boundary, streaming=streaming, task="列出最近文档，再整理目录。")
    assert run["status"] in {"failed", "partial_success"}, run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 1
    assert receipt["stop_reason"] == "execution_error" and receipt["incomplete"] is True
    assert len(requests) == 2
    assert len(receipt_boundary["attempted"]) == 1, "keyword fallback must not repeat an already executed document read"
    journal = client.get(f"/api/v1/agent/runs/{run['run_id']}/events").json()
    failed_events = [event for event in journal["items"] if event["event"] == "run.failed"]
    assert failed_events[-1]["payload"]["execution_receipt"] == receipt


@pytest.mark.parametrize("arguments", ['"{}"', '[{}]'], ids=["json-string", "json-array"])
def test_streaming_non_object_json_is_not_decoded_twice_into_executable_arguments(client, receipt_boundary, monkeypatch, arguments):
    _model_script(monkeypatch, [_reply(_call(arguments=arguments)), _reply(text="参数必须是对象。")])
    run = _run(client, receipt_boundary, streaming=True)
    assert run["status"] == "failed", run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_rejected"] == 1
    assert receipt["tool_calls_succeeded"] == 0 and receipt["incomplete"] is True
    assert receipt_boundary["attempted"] == []


def test_proposals_after_a_single_approval_pause_are_accounted_as_omitted(client, receipt_boundary, monkeypatch):
    _model_script(monkeypatch, [
        _reply(
            _call("create_memory_card", {"title": "One approval", "content": "Only this operation is approved"}, identifier="first-dangerous"),
            _call(identifier="not-preserved-after-approval"),
        ),
        _reply(text="只返回了批准的操作，另一项没有执行。"),
    ])
    pending = _run(client, receipt_boundary, streaming=True, enabled_tools=["list_recent_documents", "create_memory_card"])
    assert pending["status"] == "requires_confirmation"
    receipt = _receipt(pending)
    assert receipt["tool_calls_used"] == receipt["pending_tool_calls"] == 1
    assert receipt["omitted_tool_calls"] == 1
    assert receipt_boundary["attempted"] == [] and _memory_count(receipt_boundary) == 0
    approved = _approve(client, pending)
    assert approved.status_code == 200, approved.text
    run = approved.json()
    assert run["status"] == "partial_success", run
    receipt = _receipt(run)
    assert receipt["tool_calls_used"] == receipt["tool_calls_succeeded"] == 1
    assert receipt["omitted_tool_calls"] == 1 and receipt["pending_tool_calls"] == 0
    assert receipt["incomplete"] is True
    assert [call["action"] for call in receipt_boundary["attempted"]] == ["create_memory_card"]
    assert _memory_count(receipt_boundary) == 1


@pytest.mark.parametrize("error_flag", ["is_error", "isError"])
def test_explicit_tool_error_flag_is_failed_even_when_transport_returns_normally(client, receipt_boundary, monkeypatch, error_flag):
    value = {error_flag: True, "content": [{"type": "text", "text": "Tool rejected the operation"}]}

    def returned_error(self, **kwargs):
        receipt_boundary["attempted"].append(copy.deepcopy(kwargs))
        return value

    monkeypatch.setattr(ToolService, "execute", returned_error)
    requests = _model_script(monkeypatch, [_reply(_call()), _reply(text="工具明确返回失败。")])
    run = _run(client, receipt_boundary, streaming=True)
    assert run["status"] == "failed", run
    observed = _receipt(run)
    assert observed["tool_calls_used"] == observed["tool_calls_failed"] == 1
    assert observed["tool_calls_succeeded"] == 0 and observed["incomplete"] is True
    step = next(step for step in run["steps"] if step["step_type"] == "tool_call")
    assert step["status"] == "failed"
    model_result = json.loads(next(message for message in requests[-1]["messages"] if message["role"] == "tool")["content"])
    assert model_result["result"] == value, "the original tool error result must remain available to the model"
    assert len(receipt_boundary["attempted"]) == 1


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_initial_unsupported_provider_can_use_genuine_keyword_fallback_approval(client, receipt_boundary, monkeypatch, streaming):
    requests = _model_script(monkeypatch, [DomainValidationError("LLM_TOOLS_UNSUPPORTED")])
    pending = _run(
        client, receipt_boundary, streaming=streaming, enabled_tools=["create_memory_card"],
        task="保存为记忆：我喜欢薄荷绿。",
    )
    assert pending["status"] == "requires_confirmation", pending
    assert pending["required_confirmations"][0]["tool_name"] == "create_memory_card"
    assert pending["execution_receipt"] is None, "the keyword path must not invent an API-loop receipt"
    assert len(requests) == 1 and receipt_boundary["attempted"] == []
    assert _memory_count(receipt_boundary) == 0
    with SessionLocal() as db:
        assert db.get(AgentRun, pending["run_id"]).loop_state_json is None
    confirmed = _approve(client, pending)
    assert confirmed.status_code == 200, confirmed.text
    final = confirmed.json()
    assert final["status"] == "completed" and final["required_confirmations"] == []
    assert final["execution_receipt"] is None
    assert len(requests) == 1, "confirming a genuine keyword action must not reopen the unsupported model loop"
    assert len(receipt_boundary["attempted"]) == 1 and _memory_count(receipt_boundary) == 1
