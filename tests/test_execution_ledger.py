"""One run retains its budget and results across every approval boundary."""

import copy
import json

from pydantic import ValidationError
import pytest

from app.schemas.agent import AgentExecutionReceipt
from app.services.execution_ledger import ToolLoopLedger


SIGNATURE = ("read_note", '{"query":"private args must never reach the receipt"}')


def test_one_reservation_is_one_charge_even_after_serialized_approval_resume():
    ledger = ToolLoopLedger(3)
    assert ledger.reserve(SIGNATURE)
    ledger.stop_reason = "waiting_confirmation"
    receipt = ledger.receipt()
    assert receipt["tool_calls_used"] == 1 and receipt["pending_tool_calls"] == 1
    assert receipt["incomplete"] is True
    assert ledger.final_status() == "requires_confirmation"
    restored = ToolLoopLedger.from_state(json.loads(json.dumps(ledger.to_state())), budget=3)
    restored.resolve("completed")
    restored.stop_reason = "model_finished"
    assert restored.used == 1 and restored.pending == 0 and restored.succeeded == 1
    assert restored.final_status() == "completed"
    with pytest.raises(ValueError, match="unreserved"):
        restored.resolve("completed")


def test_repeated_approvals_share_the_same_finite_budget_and_private_history():
    ledger = ToolLoopLedger(3)
    for _ in range(3):
        assert ledger.reserve(SIGNATURE)
        ledger = ToolLoopLedger.from_state(ledger.to_state(), budget=3)
        ledger.resolve("completed")
    before = copy.deepcopy(ledger.to_state())
    assert ledger.reserve(SIGNATURE) is False
    assert ledger.to_state() == before
    assert ledger.history == [SIGNATURE] * 3
    ledger.record_omitted(1)
    ledger.stop_reason = "tool_budget"
    assert ledger.final_status() == "partial_success"
    assert ledger.receipt()["omitted_tool_calls"] == 1


def test_exact_budget_without_known_omissions_or_errors_may_finish_normally():
    ledger = ToolLoopLedger(1)
    ledger.reserve(SIGNATURE); ledger.resolve("completed"); ledger.stop_reason = "tool_budget"
    assert ledger.final_status() == "completed"
    assert ledger.receipt()["incomplete"] is False
    assert ledger.receipt()["stop_reason"] == "tool_budget"


@pytest.mark.parametrize("outcome", ["failed", "rejected"])
def test_errors_and_rejections_without_success_are_failed(outcome):
    ledger = ToolLoopLedger(3)
    ledger.reserve(SIGNATURE); ledger.resolve(outcome)
    assert ledger.final_status() == "failed"
    assert ledger.receipt()["incomplete"] is True


@pytest.mark.parametrize("progress", ["completed", "skipped"])
@pytest.mark.parametrize("bad_outcome", ["failed", "rejected"])
def test_errors_mixed_with_success_or_dry_run_are_partial(progress, bad_outcome):
    ledger = ToolLoopLedger(3)
    for outcome in (progress, bad_outcome):
        ledger.reserve(SIGNATURE); ledger.resolve(outcome)
    assert ledger.final_status() == "partial_success"
    assert ledger.receipt()["incomplete"] is True


def test_dry_run_records_skipped_calls_without_claiming_actual_tool_success():
    ledger = ToolLoopLedger(1)
    ledger.reserve(SIGNATURE); ledger.resolve("skipped")
    assert ledger.final_status() == "completed"
    assert ledger.receipt()["tool_calls_succeeded"] == 0
    assert ledger.receipt()["tool_calls_skipped"] == 1


@pytest.mark.parametrize("reason", ["duplicate_guard", "iteration_limit", "execution_error", "invalid_checkpoint"])
@pytest.mark.parametrize("successful", [False, True])
def test_abnormal_stop_does_not_masquerade_as_completion(reason, successful):
    ledger = ToolLoopLedger(3)
    if successful:
        ledger.reserve(SIGNATURE); ledger.resolve("completed")
    ledger.stop_reason = reason
    assert ledger.final_status() == ("partial_success" if successful else "failed")
    assert ledger.receipt()["incomplete"] is True


@pytest.mark.parametrize(("reason", "status"), [
    ("waiting_confirmation", "requires_confirmation"), ("cancelled", "cancelled"),
    ("interrupted", "interrupted"), ("in_progress", "running"),
])
def test_noncompletion_states_keep_their_actual_meaning(reason, status):
    ledger = ToolLoopLedger(3)
    ledger.reserve(SIGNATURE); ledger.stop_reason = reason
    assert ledger.final_status() == status
    assert ledger.receipt()["incomplete"] is True


def test_unresolved_reservation_is_incomplete_even_if_model_claims_it_finished():
    ledger = ToolLoopLedger(3); ledger.reserve(SIGNATURE)
    assert ledger.final_status() == "failed"
    assert ledger.receipt()["pending_tool_calls"] == 1


def test_reject_helper_and_omitted_calls_never_overspend():
    ledger = ToolLoopLedger(1)
    assert ledger.reject(SIGNATURE) is True
    assert ledger.reject(SIGNATURE) is False
    ledger.record_omitted(2)
    assert ledger.used == ledger.rejected == 1
    assert ledger.omitted == 2 and ledger.pending == 0


def test_public_receipt_has_a_fixed_schema_and_no_private_signature_or_arguments():
    ledger = ToolLoopLedger(3); ledger.reserve(SIGNATURE); ledger.resolve("failed")
    receipt = ledger.receipt()
    assert set(receipt) == {
        "version", "source", "tool_budget", "tool_calls_used", "tool_calls_succeeded", "tool_calls_failed",
        "tool_calls_rejected", "tool_calls_skipped", "pending_tool_calls", "omitted_tool_calls", "stop_reason", "incomplete",
    }
    assert "private args" not in json.dumps(receipt)
    assert "read_note" not in json.dumps(receipt)
    assert AgentExecutionReceipt.model_validate(receipt).model_dump() == receipt
    for extra in ("history", "errors", "arguments", "api_key"):
        with pytest.raises(ValidationError):
            AgentExecutionReceipt.model_validate({**receipt, extra: "private"})


@pytest.mark.parametrize("budget", [0, -1, 11, True, "3", 3.0, None])
def test_invalid_budgets_do_not_silently_become_fresh_allowances(budget):
    with pytest.raises(ValueError):
        ToolLoopLedger(budget)


@pytest.mark.parametrize("key", ["used", "succeeded", "failed", "rejected", "skipped", "pending", "omitted"])
@pytest.mark.parametrize("value", [-1, True, "1", 1.5])
def test_checkpoint_counts_are_strict_nonnegative_integers(key, value):
    state = ToolLoopLedger(3).to_state(); state[key] = value
    with pytest.raises(ValueError):
        ToolLoopLedger.from_state(state, budget=3)


@pytest.mark.parametrize("mutation", [
    {"version": 2}, {"version": True}, {"version": "1"}, {"budget": 2},
    {"budget": True}, {"used": 1}, {"pending": 1}, {"used": 4, "pending": 4},
    {"history": [list(SIGNATURE)]}, {"history": "not a list"},
    {"history": [["name"]]}, {"history": [["name", 1]]},
    {"stop_reason": "completely_done"}, {"stop_reason": []}, {"private_extra": "ignored?"},
])
def test_invalid_checkpoint_is_rejected_rather_than_reset(mutation):
    state = ToolLoopLedger(3).to_state(); state.update(mutation)
    with pytest.raises(ValueError):
        ToolLoopLedger.from_state(state, budget=3)


@pytest.mark.parametrize("state", [None, [], {}, "{}"])
def test_missing_checkpoint_cannot_implicitly_reset_a_live_run(state):
    with pytest.raises(ValueError):
        ToolLoopLedger.from_state(state, budget=3)


def test_checkpoint_snapshot_is_detached_from_mutable_input_and_internal_history():
    ledger = ToolLoopLedger(3); ledger.reserve(SIGNATURE)
    state = ledger.to_state(); restored = ToolLoopLedger.from_state(state, budget=3)
    state["history"][0][0] = "changed"
    assert ledger.history == [SIGNATURE] and restored.history == [SIGNATURE]


@pytest.mark.parametrize("value", [-1, True, "1", None])
def test_omitted_counter_does_not_accept_invalid_values(value):
    ledger = ToolLoopLedger(3)
    with pytest.raises(ValueError):
        ledger.record_omitted(value)
    assert ledger.omitted == 0


def test_public_schema_rejects_inconsistent_flags_counts_and_coerced_numbers():
    receipt = ToolLoopLedger(3).receipt()
    for changes in ({"tool_calls_used": 1}, {"incomplete": True}, {"tool_budget": "3"},
                    {"version": True}, {"omitted_tool_calls": True}, {"stop_reason": "unknown"}):
        with pytest.raises(ValidationError):
            AgentExecutionReceipt.model_validate({**receipt, **changes})


def test_resolve_and_signature_validation_leave_accounting_unchanged():
    ledger = ToolLoopLedger(3)
    for signature in ("raw", ("one",), ("name", 1), ["name", "args"]):
        with pytest.raises(ValueError):
            ledger.reserve(signature)
    ledger.reserve(SIGNATURE); before = ledger.to_state()
    with pytest.raises(ValueError):
        ledger.resolve("not-an-outcome")
    assert ledger.to_state() == before


@pytest.mark.parametrize("raw", [None, "", "broken json", "[]", "{}", "null", 123, []])
def test_snapshot_loader_rejects_absent_or_invalid_receipts(raw):
    assert AgentExecutionReceipt.from_snapshot(raw, run_status="completed") is None


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
def test_snapshot_loader_overlays_authoritative_stop_and_preserves_all_counts(status):
    ledger = ToolLoopLedger(3)
    ledger.reserve(SIGNATURE); ledger.resolve("completed")
    original = ledger.receipt()
    loaded = AgentExecutionReceipt.from_snapshot(json.dumps(original), run_status=status)
    assert loaded is not None
    assert loaded.model_dump() == {**original, "stop_reason": status, "incomplete": True}
    assert ledger.receipt() == original


@pytest.mark.parametrize("reason", ["in_progress", "waiting_confirmation"])
def test_snapshot_loader_marks_worker_failures_beyond_tool_loop_as_execution_error(reason):
    ledger = ToolLoopLedger(3); ledger.reserve(SIGNATURE); ledger.stop_reason = reason
    original = ledger.receipt()
    loaded = AgentExecutionReceipt.from_snapshot(json.dumps(original), run_status="failed")
    assert loaded is not None
    assert loaded.model_dump() == {**original, "stop_reason": "execution_error", "incomplete": True}


@pytest.mark.parametrize("status", ["running", "requires_confirmation", "completed", "failed"])
def test_snapshot_loader_preserves_specific_terminal_causes_and_does_not_invent_work(status):
    ledger = ToolLoopLedger(3); ledger.reject(SIGNATURE); ledger.stop_reason = "duplicate_guard"
    original = ledger.receipt()
    loaded = AgentExecutionReceipt.from_snapshot(json.dumps(original), run_status=status)
    assert loaded is not None and loaded.model_dump() == original


def test_snapshot_loader_rejects_private_fields_even_when_run_failed():
    raw = {**ToolLoopLedger(3).receipt(), "errors": ["raw credential details"]}
    assert AgentExecutionReceipt.from_snapshot(json.dumps(raw), run_status="failed") is None


@pytest.mark.parametrize("reason", ["model_finished", "tool_budget"])
def test_snapshot_loader_does_not_claim_clean_finish_after_late_plugin_or_worker_failure(reason):
    ledger = ToolLoopLedger(1)
    ledger.reserve(SIGNATURE); ledger.resolve("completed"); ledger.stop_reason = reason
    original = ledger.receipt()
    assert original["incomplete"] is False
    loaded = AgentExecutionReceipt.from_snapshot(json.dumps(original), run_status="failed")
    assert loaded is not None
    assert loaded.model_dump() == {**original, "stop_reason": "execution_error", "incomplete": True}
