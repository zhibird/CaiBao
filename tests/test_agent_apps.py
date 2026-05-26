from uuid import uuid4

from app.db.session import SessionLocal
from app.models.incident import Incident
from tests.auth_helpers import register_workspace_user


def _workspace(client, prefix: str) -> tuple[str, str, str]:
    team_id, user_id = register_workspace_user(
        client,
        prefix=prefix,
        display_name="Agent App User",
    )
    response = client.post("/api/v1/conversations", json={"title": "Agent App Session"})
    assert response.status_code == 201
    return team_id, user_id, response.json()["conversation_id"]


def _incident_count(team_id: str) -> int:
    with SessionLocal() as db:
        return len(db.query(Incident).filter(Incident.team_id == team_id).all())


def test_agent_app_crud_publish_and_invoke_plain_question(client) -> None:
    team_id, user_id, conversation_id = _workspace(client, f"app_crud_{uuid4().hex[:6]}")

    create = client.post(
        "/api/v1/apps",
        json={
            "name": "运维值班助手",
            "description": "Dify-lite app",
            "system_prompt": "你是企业运维知识 Agent。",
            "retrieval_config": {"top_k": 3, "include_memory": False, "include_library": False},
            "tool_config": {"enabled_tools": ["list_recent_documents"], "dangerous_requires_confirmation": True},
        },
    )
    assert create.status_code == 201
    app = create.json()
    assert app["team_id"] == team_id
    assert app["created_by_user_id"] == user_id
    assert app["mode"] == "agent_auto"
    assert app["app_version"] == 0

    listing = client.get("/api/v1/apps")
    assert listing.status_code == 200
    assert any(item["app_id"] == app["app_id"] for item in listing.json()["items"])

    update = client.patch(
        f"/api/v1/apps/{app['app_id']}",
        json={"name": "数据库告警助手", "mode": "agent_auto"},
    )
    assert update.status_code == 200
    assert update.json()["name"] == "数据库告警助手"

    publish = client.post(f"/api/v1/apps/{app['app_id']}/publish", json={"notes": "baseline"})
    assert publish.status_code == 200
    assert publish.json()["version_number"] == 1
    assert publish.json()["snapshot"]["name"] == "数据库告警助手"

    invoke = client.post(
        f"/api/v1/apps/{app['app_id']}/invoke",
        json={
            "conversation_id": conversation_id,
            "task": "请解释一下这个工作台能做什么，不要创建事件。",
            "include_memory": False,
            "include_library": False,
        },
    )
    assert invoke.status_code == 200
    body = invoke.json()
    assert body["app_id"] == app["app_id"]
    assert body["app_version"] == 1
    assert body["trigger_channel"] == "app"
    assert body["status"] == "completed"
    assert [step["step_type"] for step in body["steps"]] == ["plan", "retrieval", "final"]


def test_agent_app_blocks_dangerous_tool_until_confirmed(client) -> None:
    team_id, _, conversation_id = _workspace(client, f"app_block_{uuid4().hex[:6]}")
    before_count = _incident_count(team_id)

    create = client.post(
        "/api/v1/apps",
        json={
            "name": "Incident App",
            "tool_config": {"enabled_tools": ["create_incident"], "dangerous_requires_confirmation": True},
            "retrieval_config": {"include_memory": False, "include_library": False},
        },
    )
    assert create.status_code == 201
    app = create.json()

    invoke = client.post(
        f"/api/v1/apps/{app['app_id']}/invoke",
        json={
            "conversation_id": conversation_id,
            "task": "数据库 CPU 告警了，创建一个 P1 incident：Database CPU usage above threshold",
            "include_memory": False,
            "include_library": False,
        },
    )
    assert invoke.status_code == 200
    body = invoke.json()
    assert body["status"] == "requires_confirmation"
    assert body["required_confirmations"][0]["tool_name"] == "create_incident"
    assert _incident_count(team_id) == before_count

    confirm = client.post(f"/api/v1/agent/runs/{body['run_id']}/confirm", json={})
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "completed"
    assert _incident_count(team_id) == before_count + 1


def test_agent_app_workflow_mode_records_node_trace_and_blocks_write(client) -> None:
    team_id, _, conversation_id = _workspace(client, f"app_workflow_{uuid4().hex[:6]}")
    before_count = _incident_count(team_id)

    create = client.post(
        "/api/v1/apps",
        json={
            "name": "Workflow Incident App",
            "mode": "workflow",
            "tool_config": {"enabled_tools": ["create_incident"], "dangerous_requires_confirmation": True},
            "retrieval_config": {"include_memory": False, "include_library": False},
            "workflow_config": {
                "nodes": [
                    {"type": "retrieval", "title": "查处理手册"},
                    {
                        "type": "tool_call",
                        "title": "创建 P1 事件",
                        "tool_name": "create_incident",
                        "arguments": {"title": "数据库 CPU 告警", "severity": "P1"},
                    },
                    {"type": "final", "title": "汇总结果"},
                ]
            },
        },
    )
    assert create.status_code == 201
    app = create.json()

    invoke = client.post(
        f"/api/v1/apps/{app['app_id']}/invoke",
        json={
            "conversation_id": conversation_id,
            "task": "数据库 CPU 告警了，按工作流处理。",
            "include_memory": False,
            "include_library": False,
        },
    )
    assert invoke.status_code == 200
    body = invoke.json()
    assert body["status"] == "requires_confirmation"
    assert _incident_count(team_id) == before_count
    assert [step["step_type"] for step in body["steps"]] == ["plan", "retrieval", "tool_call", "final"]
    assert body["steps"][2]["title"] == "创建 P1 事件"
