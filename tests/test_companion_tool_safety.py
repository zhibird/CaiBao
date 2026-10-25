"""Companion channel continuity and enforcement of tool permissions."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.session import SessionLocal
from app.schemas.agent import AgentRunRequest, AgentRunResponse, AgentToolCall
from app.services.agent_service import AgentService
from app.services.chat_history_service import ChatHistoryService
from app.services.llm_service import LLMCompletionResult, LLMService, LLMStreamChunk
from app.services.persona_profiles import resolve_persona_name
from tests.auth_helpers import register_workspace_user


class _Definition:
    dangerous = False

    def normalize_arguments(self, arguments):
        return arguments

    def validate_arguments(self, arguments):
        return []


class _Tools:
    def __init__(self, dangerous=False):
        self.executed = []
        self.definition = _Definition()
        self.definition.dangerous = dangerous

    def get_tool_definition(self, name):
        return self.definition

    def execute(self, **kwargs):
        self.executed.append(kwargs["action"])
        return {"ok": True}


class _HallucinatingLLM:
    """Request a real tool even when its schema was never offered."""

    finalize_tool_calls_from_stream = LLMService.finalize_tool_calls_from_stream

    def __init__(self):
        self.calls = 0

    def complete_chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMCompletionResult(
                assistant_text="",
                tool_calls=[{
                    "id": "call-forbidden",
                    "type": "function",
                    "function": {"name": "write_note", "arguments": "{}"},
                }],
            )
        return LLMCompletionResult(assistant_text="Finished.")

    def stream_chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield LLMStreamChunk(tool_call_deltas=[{
                "index": 0,
                "function": {"name": "write_note", "arguments": "{}"},
            }])
        else:
            yield LLMStreamChunk(delta_text="Finished.")


def _run_hallucinated_tool(*, streaming, enabled_tools, dangerous=False):
    service = object.__new__(AgentService)
    service.llm_service = _HallucinatingLLM()
    service.tool_service = _Tools(dangerous=dangerous)
    service.event_bus = None
    service._record_step = lambda **kwargs: None
    service._run_plugin_phase = lambda *args, **kwargs: None
    finished = []
    service._finish_run = lambda **kwargs: finished.append(kwargs)
    service._next_step_index = lambda run_id: 50
    # A forbidden dangerous call must not even become an approvable pending call.
    service._save_pending_tool_call = lambda *args: pytest.fail("Forbidden tool queued for approval")
    messages = [{"role": "user", "content": "Just chat with me."}]
    arguments = dict(
        run=SimpleNamespace(run_id="run-1", task="chat", team_id="t", user_id="u", space_id=None),
        messages=messages,
        tools=[],
        team_id="t",
        user_id="u",
        space_id=None,
        base_url="",
        api_key="",
        model_name="test",
        started=0,
        enable_tool_execution=True,
        confirm_dangerous_actions=not dangerous,
        dry_run=False,
        enabled_tools=enabled_tools,
    )
    expected_status = "completed" if enabled_tools is None or "write_note" in {
        item.strip().lower() for item in enabled_tools
    } else "failed"
    if streaming:
        events = list(service._tool_loop_stream(**arguments, start_seq=0))
        assert finished[-1]["status"] == expected_status
    else:
        answer, status, requires_confirmation = service._tool_loop(**arguments, step_index=1)
        assert answer == "Finished."
        assert status == expected_status
        assert requires_confirmation is False
        events = []
    return service, messages, events


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
@pytest.mark.parametrize("enabled_tools", [[], ["", "  "], ["read_note"]])
@pytest.mark.parametrize("dangerous", [False, True], ids=["read", "write"])
def test_forbidden_hallucinated_tool_never_executes(streaming, enabled_tools, dangerous):
    service, messages, events = _run_hallucinated_tool(
        streaming=streaming, enabled_tools=enabled_tools, dangerous=dangerous,
    )

    assert service.tool_service.executed == []
    errors = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
    assert len(errors) == 1
    assert errors[0]["error"] == "Tool 'write_note' is not enabled for this run."
    assert errors[0]["rejected"] is True
    assert not {event.event for event in events} & {"tool.started", "tool.result", "confirmation.required"}


@pytest.mark.parametrize("streaming", [False, True], ids=["sync", "stream"])
@pytest.mark.parametrize("enabled_tools", [None, [" WRITE_NOTE "]])
def test_unrestricted_or_explicitly_allowed_tool_still_executes(streaming, enabled_tools):
    service, _, _ = _run_hallucinated_tool(streaming=streaming, enabled_tools=enabled_tools)

    assert service.tool_service.executed == ["write_note"]


@pytest.mark.parametrize("channel", ["companion", " COMPANION "])
def test_desktop_companion_uses_companion_persona(channel):
    assert resolve_persona_name(channel) == "companion"


def test_companion_history_survives_reload_and_enters_next_turn(client):
    team_id, user_id = register_workspace_user(
        client, prefix=f"pet_history_{uuid4().hex[:6]}", display_name="Companion User",
    )
    conversation = client.post("/api/v1/conversations", json={"title": "Companion"})
    assert conversation.status_code == 201
    conversation_id = conversation.json()["conversation_id"]
    payload = AgentRunRequest(
        team_id=team_id, user_id=user_id, conversation_id=conversation_id,
        trigger_channel="companion", task="请记得我喜欢薄荷绿。", enabled_tools=[],
    )
    response = AgentRunResponse(
        run_id=str(uuid4()), team_id=team_id, user_id=user_id,
        conversation_id=conversation_id, space_id=None, trigger_channel="companion",
        task=payload.task, status="completed", answer="嗯，薄荷绿。",
        dry_run=False, max_steps=5, created_at=datetime.now(timezone.utc),
    )
    with SessionLocal() as db:
        service = object.__new__(AgentService)
        service.db = db
        service.chat_history_service = ChatHistoryService(db)
        service._record_agent_history(payload=payload, response=response)

    # New database session proves the next turn reads persisted conversation data.
    with SessionLocal() as db:
        service = object.__new__(AgentService)
        service.db = db
        service.chat_history_service = ChatHistoryService(db)
        service.llm_service = SimpleNamespace(settings=SimpleNamespace(llm_history_turns=5))
        stored = service.chat_history_service.list_history(
            team_id=team_id, user_id=user_id, conversation_id=conversation_id,
        )
        assert len(stored) == 1
        assert stored[0].channel == "companion"
        assert json.loads(stored[0].request_payload_json)["enabled_tools"] == []
        messages = service._build_agent_conversation_messages(
            team_id=team_id, user_id=user_id, conversation_id=conversation_id,
        )
    assert messages[0]["role"] == "system"
    assert "Past conversation" in messages[0]["content"]
    assert messages[1:] == [
            {"role": "user", "content": "请记得我喜欢薄荷绿。"},
            {"role": "assistant", "content": "嗯，薄荷绿。"},
        ]


def test_streaming_keyword_fallback_uses_next_step_for_tool_events():
    service = object.__new__(AgentService)
    service.db = SimpleNamespace(scalar=lambda query: None)
    service.tool_service = _Tools()
    events = []
    service.event_bus = SimpleNamespace(emit=events.append)
    service._load_request_payload = lambda run: AgentRunRequest(task=run.task)
    service._plan_tool_calls = lambda *args, **kwargs: [AgentToolCall(tool_name="read_note")]
    service._next_step_index = lambda run_id: 7
    service._record_step = lambda **kwargs: None
    service._finish_run = lambda **kwargs: None
    service._build_final_answer = lambda **kwargs: "Fallback completed."

    answer = service._run_agent_auto_fallback_sync(
        run=SimpleNamespace(run_id="fallback-run", task="read my note", dry_run=False),
        team_id="t", user_id="u", space_id=None, started=0,
    )

    assert answer == "Fallback completed."
    assert service.tool_service.executed == ["read_note"]
    assert len(events) == 2
    assert all(event.step_index == 7 for event in events)
