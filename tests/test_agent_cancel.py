"""Cooperative stop remains authoritative across separate request sessions."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.services.agent_service import AgentService
from app.services.llm_service import LLMCompletionResult, LLMService, LLMStreamChunk
from app.services.tool_registry import get_tool_definition
from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


def _queued(client, *, status="queued", mode="agent_auto"):
    team_id, user_id = register_workspace_user(
        client, prefix=f"stop_{uuid4().hex[:6]}", display_name="Stop test",
    )
    queued = client.post("/api/v1/agent/runs", json={
        "task": "Read a document.", "trigger_channel": "companion", "run_mode": mode,
        "include_memory": False, "include_library": False,
    })
    assert queued.status_code == 200
    run_id = queued.json()["run_id"]
    if status != "queued":
        with SessionLocal() as db:
            run = db.get(AgentRun, run_id)
            run.status = status
            if status == "requires_confirmation":
                run.required_confirmations_json = json.dumps([{
                    "tool_name": "create_memory_card", "arguments": {"title": "Never execute"},
                    "dangerous": True, "requires_confirmation": True,
                }])
                run.loop_state_json = "{}"
            db.commit()
    return team_id, user_id, run_id, queued.json()["stream_url"]


def _minimal_service(db):
    service = object.__new__(AgentService)
    service.db = db
    service.event_bus = None
    service.user_service = SimpleNamespace(ensure_user_in_team=lambda **kwargs: None)
    return service


def _cancel_elsewhere(team_id, user_id, run_id):
    with SessionLocal() as db:
        response = _minimal_service(db).cancel_run(run_id=run_id, team_id=team_id, user_id=user_id)
        assert response.status == "cancelled"


@pytest.mark.parametrize("status", ["queued", "requires_confirmation", "running"])
def test_cancel_endpoint_clears_pending_work_and_prevents_confirmation(client, status):
    _, _, run_id, stream_url = _queued(client, status=status)
    cancelled = client.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["required_confirmations"] == []
    assert client.post(f"/api/v1/agent/runs/{run_id}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/v1/agent/runs/{run_id}/confirm", json={}).status_code == 400
    replay = client.get(f"/api/v1{stream_url}")
    assert "event: run.cancelled" in replay.text
    assert "event: run.completed" not in replay.text
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        assert run.loop_state_json is None
        assert run.completed_at is not None


def test_cancel_requires_authentication_and_hides_teammates_runs(client):
    team_id, _, run_id, _ = _queued(client)
    register_workspace_user_in_team(client, team_id=team_id, prefix="stop_other", display_name="Other")
    assert client.post(f"/api/v1/agent/runs/{run_id}/cancel").status_code == 404
    with SessionLocal() as db:
        assert db.get(AgentRun, run_id).status == "queued"
    client.cookies.clear()
    assert client.post(f"/api/v1/agent/runs/{run_id}/cancel").status_code == 401


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_finished_run_cancel_is_idempotent(client, status):
    _, _, run_id, _ = _queued(client, status=status)
    response = client.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == status


@pytest.mark.parametrize("mode", ["codex", "workflow"])
def test_running_external_modes_do_not_claim_they_can_stop(client, mode):
    _, _, run_id, _ = _queued(client, status="running", mode=mode)
    response = client.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert response.status_code == 400
    assert client.get(f"/api/v1/agent/runs/{run_id}").json()["status"] == "running"


def test_stream_cancellation_from_other_session_stops_new_text_and_tool_proposals(client, monkeypatch):
    team_id, user_id, run_id, stream_url = _queued(client)

    def interrupted_stream(self, **kwargs):
        yield LLMStreamChunk(delta_text="Before stop.")
        _cancel_elsewhere(team_id, user_id, run_id)
        yield LLMStreamChunk(delta_text="MUST-NOT-BE-EMITTED")
        yield LLMStreamChunk(tool_call_deltas=[{
            "index": 0, "function": {"name": "create_memory_card", "arguments": "{}"},
        }])

    monkeypatch.setattr(LLMService, "stream_chat", interrupted_stream)
    response = client.get(f"/api/v1{stream_url}")
    assert response.status_code == 200
    assert "Before stop." in response.text
    assert "MUST-NOT-BE-EMITTED" not in response.text
    assert "event: run.cancelled" in response.text
    assert "event: run.completed" not in response.text
    assert "event: tool.proposed" not in response.text
    assert client.get(f"/api/v1/agent/runs/{run_id}").json()["status"] == "cancelled"


def _loop_arguments(run):
    return dict(
        run=run, messages=[{"role": "user", "content": "read"}], tools=[],
        team_id=run.team_id, user_id=run.user_id, space_id=run.space_id,
        base_url="", api_key="", model_name="test", started=0,
        enable_tool_execution=True, confirm_dangerous_actions=False, dry_run=False,
        enabled_tools=["list_recent_documents"],
    )


def test_cancel_after_synchronous_model_response_prevents_tool_execution(client):
    team_id, user_id, run_id, _ = _queued(client, status="running")
    executed = []

    def model_response(**kwargs):
        _cancel_elsewhere(team_id, user_id, run_id)
        return LLMCompletionResult(assistant_text="", tool_calls=[{
            "id": "late-tool", "type": "function",
            "function": {"name": "list_recent_documents", "arguments": "{}"},
        }])

    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        service = _minimal_service(db)
        service.llm_service = SimpleNamespace(complete_chat=model_response)
        service.tool_service = SimpleNamespace(execute=lambda **kwargs: executed.append(kwargs))
        _, status, pending = service._tool_loop(**_loop_arguments(run), step_index=1)
        assert status == "cancelled"
        assert pending is False
        assert executed == []


def test_stop_after_tool_started_frame_prevents_the_actual_handler(client):
    team_id, user_id, run_id, _ = _queued(client, status="running")
    executed = []

    def proposed_tool(**kwargs):
        yield LLMStreamChunk(tool_call_deltas=[{
            "index": 0, "function": {"name": "list_recent_documents", "arguments": "{}"},
        }])

    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        service = _minimal_service(db)
        service.llm_service = SimpleNamespace(
            stream_chat=proposed_tool,
            finalize_tool_calls_from_stream=LLMService().finalize_tool_calls_from_stream,
        )
        service.tool_service = SimpleNamespace(
            get_tool_definition=get_tool_definition,
            execute=lambda **kwargs: executed.append(kwargs),
        )
        events = service._tool_loop_stream(**_loop_arguments(run), start_seq=0)
        assert next(events).event == "tool.proposed"
        assert next(events).event == "tool.started"
        _cancel_elsewhere(team_id, user_id, run_id)
        assert list(events) == []
        assert executed == []


def test_late_finish_cannot_overwrite_stop_committed_by_another_session(client):
    team_id, user_id, run_id, _ = _queued(client, status="running")
    with SessionLocal() as db:
        service = _minimal_service(db)
        stale_run = db.get(AgentRun, run_id)
        assert stale_run.status == "running"
        _cancel_elsewhere(team_id, user_id, run_id)
        service._finish_run(
            run=stale_run, status="completed", final_answer="MUST-NOT-OVERWRITE-STOP",
            required_confirmations=[], started=0,
        )
        assert stale_run.status == "cancelled"
        assert "MUST-NOT-OVERWRITE-STOP" not in stale_run.final_answer


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
def test_active_tool_can_finish_but_the_next_tool_never_starts(client, streaming):
    team_id, user_id, run_id, _ = _queued(client, status="running")
    executed = []

    def active_tool(**kwargs):
        executed.append(kwargs)
        _cancel_elsewhere(team_id, user_id, run_id)
        return {"already_started": "finished"}

    def proposed_tools(**kwargs):
        yield LLMStreamChunk(tool_call_deltas=[
            {"index": index, "function": {"name": "list_recent_documents", "arguments": json.dumps({"limit": index + 1})}}
            for index in range(2)
        ])

    with SessionLocal() as db:
        service = _minimal_service(db)
        run = db.get(AgentRun, run_id)
        service.tool_service = SimpleNamespace(get_tool_definition=get_tool_definition, execute=active_tool)
        service.llm_service = SimpleNamespace(
            complete_chat=lambda **kwargs: LLMCompletionResult(assistant_text="", tool_calls=[
                {"id": f"call-{index}", "type": "function", "function": {
                    "name": "list_recent_documents", "arguments": json.dumps({"limit": index + 1}),
                }} for index in range(2)
            ]),
            stream_chat=proposed_tools,
            finalize_tool_calls_from_stream=LLMService().finalize_tool_calls_from_stream,
        )
        if streaming:
            events = list(service._tool_loop_stream(**_loop_arguments(run), start_seq=0))
            assert [event.event for event in events] == ["tool.proposed", "tool.started"]
        else:
            _, status, _ = service._tool_loop(**_loop_arguments(run), step_index=1)
            assert status == "cancelled"
        assert len(executed) == 1
        assert executed[0]["arguments"]["limit"] == 1
        # The completed in-flight work is audited even though later work stops.
        assert len(service._load_steps(run_id)) == 1
        assert service._load_steps(run_id)[0].status == "completed"


def test_second_stream_does_not_execute_or_claim_a_running_run_completed(client, monkeypatch):
    _, _, run_id, stream_url = _queued(client)

    def must_not_execute(*args, **kwargs):
        pytest.fail("A second stream must not execute the running task")

    monkeypatch.setattr(LLMService, "stream_chat", must_not_execute)
    with SessionLocal() as db:
        service = _minimal_service(db)
        service.document_service = SimpleNamespace(resolve_space_id=lambda **kwargs: None)
        service._prepare_execution = lambda *args: {}
        first_stream = service.stream_run(run_id)
        assert next(first_stream).event == "run.started"
        second_stream = client.get(f"/api/v1{stream_url}")
        assert second_stream.status_code == 200
        assert "event: run.in_progress" in second_stream.text
        assert "event: run.completed" not in second_stream.text
        assert client.get(f"/api/v1/agent/runs/{run_id}").json()["status"] == "running"
        first_stream.close()
