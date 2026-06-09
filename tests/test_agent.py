import json
from types import SimpleNamespace
from uuid import uuid4

from app.db.session import SessionLocal
from app.models.incident import Incident
from app.services.agent_service import AgentService
from app.services.llm_service import LLMCompletionResult
from app.services.persona_prompt import PersonaPromptParts
from tests.auth_helpers import register_workspace_user


def _create_workspace(client, prefix: str) -> tuple[str, str, str]:
    team_id, user_id = register_workspace_user(
        client,
        prefix=prefix,
        display_name="Agent User",
    )
    response = client.post("/api/v1/conversations", json={"title": "Agent Session"})
    assert response.status_code == 201
    return team_id, user_id, response.json()["conversation_id"]


def _incident_count(team_id: str) -> int:
    with SessionLocal() as db:
        return len(db.query(Incident).filter(Incident.team_id == team_id).all())


def test_agent_tool_call_history_serializes_function_arguments() -> None:
    message = AgentService._assistant_tool_call_message(
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": {"query": "怕上火暴王老菊 最新视频", "limit": 5},
            },
        }
    )

    arguments = message["tool_calls"][0]["function"]["arguments"]  # type: ignore[index]
    assert isinstance(arguments, str)

    reasoning_message = AgentService._assistant_tool_call_message(
        {
            "id": "call_2",
            "type": "function",
            "function": {
                "name": "web_search",
                "arguments": {"query": "latest video", "limit": 5},
            },
            "_assistant_reasoning_content": "thinking trace",
        }
    )
    reasoning_arguments = reasoning_message["tool_calls"][0]["function"]["arguments"]  # type: ignore[index]
    assert json.loads(reasoning_arguments) == {"query": "latest video", "limit": 5}
    assert reasoning_message["reasoning_content"] == "thinking trace"
    assert "_assistant_reasoning_content" not in reasoning_message["tool_calls"][0]  # type: ignore[index]


def test_agent_tool_loop_stops_requesting_tools_after_budget() -> None:
    class _Definition:
        dangerous = False

        def normalize_arguments(self, arguments):  # noqa: ANN001
            return arguments

        def validate_arguments(self, arguments):  # noqa: ANN001
            return []

    class _ToolService:
        def __init__(self) -> None:
            self.executed = 0

        def get_tool_definition(self, tool_name):  # noqa: ANN001
            return _Definition()

        def execute(self, **kwargs):  # noqa: ANN003
            self.executed += 1
            return {"results": [{"title": "latest"}]}

    class _LLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def complete_chat(self, **kwargs):  # noqa: ANN003
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LLMCompletionResult(
                    assistant_text="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": {"query": "latest", "limit": 5},
                            },
                        }
                    ],
                )
            return LLMCompletionResult(assistant_text="final answer")

    service = object.__new__(AgentService)
    service.llm_service = _LLM()
    service.tool_service = _ToolService()
    service.event_bus = None
    service._record_step = lambda **kwargs: None  # type: ignore[method-assign]

    final_answer, status, requires_confirmation = service._tool_loop(
        run=SimpleNamespace(run_id="run-1", task="find latest"),
        messages=[{"role": "user", "content": "find latest"}],
        tools=[{"type": "function", "function": {"name": "web_search"}}],
        team_id="team-1",
        user_id="user-1",
        space_id=None,
        base_url="",
        api_key="",
        model_name="model",
        step_index=1,
        started=0,
        enable_tool_execution=True,
        confirm_dangerous_actions=True,
        dry_run=False,
        max_tool_calls=1,
    )

    assert final_answer == "final answer"
    assert status == "completed"
    assert requires_confirmation is False
    assert service.tool_service.executed == 1
    assert len(service.llm_service.calls) == 2
    assert service.llm_service.calls[1]["tools"] is None
    messages = service.llm_service.calls[1]["messages"]
    assert "Tool-call budget reached" in messages[-1]["content"]  # type: ignore[index]


def test_agent_messages_include_qqbot_conversation_history() -> None:
    class _History:
        def list_messages_for_context(self, **kwargs):  # noqa: ANN003
            return [
                SimpleNamespace(
                    channel="qqbot",
                    request_text="帮我查一下B站UP主，怕上火暴王老菊 最新发的一期视频叫什么名字？",
                    response_text="最新一期视频是《王老菊教你暴食喷王》。",
                ),
                SimpleNamespace(
                    channel="action",
                    request_text="internal action",
                    response_text="hidden",
                ),
            ]

    class _Persona:
        def build(self, **kwargs):  # noqa: ANN003
            return PersonaPromptParts(
                system_prompt="system",
                section_labels=("identity",),
                persona_name="test",
            )

    service = object.__new__(AgentService)
    service.chat_history_service = _History()
    service.llm_service = SimpleNamespace(settings=SimpleNamespace(llm_history_turns=5))
    service.persona_prompt_builder = _Persona()

    messages = service._build_agent_messages(
        task="这个up主的真名叫什么？",
        system_prompt=None,
        rag_answer="",
        team_id="team-1",
        user_id="user-1",
        conversation_id="conv-1",
        include_memory=True,
        channel="qqbot",
    )

    assert messages[0] == {"role": "system", "content": "system"}
    assert messages[1]["role"] == "user"
    assert "怕上火暴王老菊" in messages[1]["content"]
    assert messages[2] == {
        "role": "assistant",
        "content": "最新一期视频是《王老菊教你暴食喷王》。",
    }
    assert "internal action" not in str(messages)
    assert messages[-1]["role"] == "user"
    assert "这个up主的真名叫什么？" in messages[-1]["content"]


def test_agent_run_plain_question_does_not_call_tools(client) -> None:
    team_id, user_id, conversation_id = _create_workspace(client, f"agent_plain_{uuid4().hex[:6]}")

    response = client.post(
        "/api/v1/agent/run",
        json={
            "conversation_id": conversation_id,
            "task": "请解释一下这个工作台能做什么，不要创建事件。",
            "include_memory": False,
            "include_library": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["team_id"] == team_id
    assert body["user_id"] == user_id
    assert body["conversation_id"] == conversation_id
    assert body["status"] == "completed"
    assert body["tool_calls"] == []
    assert [step["step_type"] for step in body["steps"]] == ["plan", "retrieval", "final"]

    history = client.get("/api/v1/chat/history", params={"conversation_id": conversation_id, "limit": 10})
    assert history.status_code == 200
    assert history.json()["items"][0]["channel"] == "agent"


def test_agent_run_blocks_dangerous_incident_until_confirmed(client) -> None:
    team_id, _, conversation_id = _create_workspace(client, f"agent_block_{uuid4().hex[:6]}")
    before_count = _incident_count(team_id)

    response = client.post(
        "/api/v1/agent/run",
        json={
            "conversation_id": conversation_id,
            "task": "数据库 CPU 告警了，创建一个 P1 incident：Database CPU usage above threshold",
            "include_memory": False,
            "include_library": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "requires_confirmation"
    assert body["required_confirmations"][0]["tool_name"] == "create_incident"
    assert body["required_confirmations"][0]["arguments"]["severity"] == "P1"
    assert _incident_count(team_id) == before_count

    run_lookup = client.get(f"/api/v1/agent/runs/{body['run_id']}")
    assert run_lookup.status_code == 200
    assert run_lookup.json()["status"] == "requires_confirmation"

    confirm = client.post(f"/api/v1/agent/runs/{body['run_id']}/confirm", json={})
    assert confirm.status_code == 200
    confirmed = confirm.json()
    assert confirmed["status"] == "completed"
    assert confirmed["required_confirmations"] == []
    assert _incident_count(team_id) == before_count + 1


def test_agent_run_dry_run_does_not_create_incident(client) -> None:
    team_id, _, conversation_id = _create_workspace(client, f"agent_dry_{uuid4().hex[:6]}")
    before_count = _incident_count(team_id)

    response = client.post(
        "/api/v1/agent/run",
        json={
            "conversation_id": conversation_id,
            "task": "Dry run：创建一个 P2 incident：Cache latency degraded",
            "dry_run": True,
            "include_memory": False,
            "include_library": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["dry_run"] is True
    assert any(step["status"] == "skipped" for step in body["steps"])
    assert _incident_count(team_id) == before_count


def test_agent_run_invalid_tool_arguments_fail_without_dirty_write(client) -> None:
    team_id, _, conversation_id = _create_workspace(client, f"agent_invalid_{uuid4().hex[:6]}")
    before_count = _incident_count(team_id)

    response = client.post(
        "/api/v1/agent/run",
        json={
            "conversation_id": conversation_id,
            "task": "创建一个 P9 incident：Invalid severity example",
            "confirm_dangerous_actions": True,
            "include_memory": False,
            "include_library": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    # With LLM tool loop, the mock LLM attempts the tool call, which fails.
    # The LLM then synthesizes a final answer instead of hard-erroring.
    # The key invariant: no dirty write — invalid severity does not create an incident.
    assert body["status"] in {"completed", "partial_success", "failed"}
    assert _incident_count(team_id) == before_count


def test_agent_run_can_execute_safe_recent_documents_tool(client) -> None:
    team_id, _, conversation_id = _create_workspace(client, f"agent_docs_{uuid4().hex[:6]}")
    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "conversation_id": conversation_id,
            "source_name": "agent-ops.md",
            "content_type": "md",
            "content": "Agent demo document.",
        },
    )
    assert import_response.status_code == 201

    response = client.post(
        "/api/v1/agent/run",
        json={
            "conversation_id": conversation_id,
            "task": "查最近文档，limit 1",
            "include_memory": False,
            "include_library": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["team_id"] == team_id
    assert body["status"] == "completed"
    assert body["tool_calls"][0]["tool_name"] == "list_recent_documents"
    tool_step = next(step for step in body["steps"] if step["step_type"] == "tool_call")
    documents = tool_step["output"]["executed"][0]["result"]["documents"]
    assert documents[0]["source_name"] == "agent-ops.md"


def test_agent_tools_endpoint_exposes_registry(client) -> None:
    _create_workspace(client, f"agent_tools_{uuid4().hex[:6]}")

    response = client.get("/api/v1/agent/tools")

    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()}
    assert tools["create_incident"]["dangerous"] is True
    assert tools["list_recent_documents"]["dangerous"] is False
