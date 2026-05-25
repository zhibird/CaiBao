from uuid import uuid4

from app.db.session import SessionLocal
from app.models.incident import Incident
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
    assert body["status"] == "failed"
    assert "severity must be one of" in body["answer"]
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
