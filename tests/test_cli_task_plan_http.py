"""Real HTTP, subprocess parsing, durable plans and history with inert fake CLIs.

The child programs only read stdin and write JSONL. They never launch tools,
read user files, contact a model, or use an installed agent CLI.
"""

import json
import shlex
import stat
import sys

import pytest

from app.core.config import reload_settings
from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from tests.auth_helpers import register_workspace_user


_FAKE_CLI = r'''
import json
import sys

def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()

task = sys.stdin.read().strip().splitlines()[-1]

if "--output-format" in sys.argv:
    emit({"type": "system", "subtype": "init", "session_id": "PRIVATE-CLAUDE-SESSION"})

    def use(identifier, name, arguments):
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": identifier, "name": name, "input": arguments}
        ]}})

    def result(identifier, output):
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": identifier, "is_error": False,
             "content": [{"type": "text", "text": "inert metadata response"}]}
        ]}, "tool_use_result": output})

    use("PRIVATE-CREATE-CALL", "TaskCreate", {
        "subject": "核对结构化资料", "description": "PRIVATE-DESCRIPTION",
        "activeForm": "PRIVATE-ACTIVE-FORM",
    })
    result("PRIVATE-CREATE-CALL", {"task": {"id": "PRIVATE-TASK-ID", "subject": "核对结构化资料"}})
    use("PRIVATE-UPDATE-CALL", "TaskUpdate", {"taskId": "PRIVATE-TASK-ID", "status": "in_progress"})
    result("PRIVATE-UPDATE-CALL", {
        "success": True, "taskId": "PRIVATE-TASK-ID", "updatedFields": ["status"],
        "statusChange": {"from": "pending", "to": "in_progress"},
    })
    # Normal transport success can still contain an explicit metadata failure.
    # This must not mark the task completed or create another public revision.
    use("PRIVATE-FAILED-UPDATE", "TaskUpdate", {"taskId": "PRIVATE-TASK-ID", "status": "completed"})
    result("PRIVATE-FAILED-UPDATE", {
        "success": False, "taskId": "PRIVATE-TASK-ID", "updatedFields": [],
        "error": "PRIVATE-METADATA-FAILURE", "statusChange": None,
    })
    emit({"type": "result", "is_error": False, "result": "假 Claude 已返回；计划仍报告正在核对。",
          "session_id": "PRIVATE-CLAUDE-SESSION"})
else:
    emit({"type": "thread.started", "thread_id": "PRIVATE-CODEX-SESSION"})
    if task != "SECOND_WITHOUT_PLAN":
        failed = task == "CODEX_FAILED_CLAIMED_DONE"
        initial = [{"text": "核对结构化资料", "completed": failed},
                   {"text": "整理结果", "completed": failed}]
        emit({"type": "item.started", "item": {"id": "PRIVATE-TODO-ID", "type": "todo_list", "items": initial}})
        updated = [{"text": "核对结构化资料", "completed": True},
                   {"text": "整理结果", "completed": failed}]
        emit({"type": "item.updated", "item": {"id": "PRIVATE-TODO-ID", "type": "todo_list", "items": updated}})
        emit({"type": "item.completed", "item": {"id": "PRIVATE-TODO-ID", "type": "todo_list", "items": updated}})
    emit({"type": "item.completed", "item": {"id": "PRIVATE-REASONING-ID", "type": "reasoning", "text": "PRIVATE-REASONING"}})
    emit({"type": "item.completed", "item": {"id": "PRIVATE-ANSWER-ID", "type": "agent_message", "text": "假 Codex 返回的回答。"}})
    if task == "CODEX_FAILED_CLAIMED_DONE":
        emit({"type": "turn.failed", "error": {"message": "LOCAL-INERT-CLI-FAILURE"}})
    else:
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
'''


@pytest.fixture
def fake_cli_plan_boundary(client, monkeypatch, tmp_path):
    script = tmp_path / "inert_cli.py"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    executable = tmp_path / "inert_cli"
    executable.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + ' "$@"\n',
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    for key, value in {
        "CLI_AGENT_ENABLED": "true", "CLI_AGENT_CODEX_PATH": str(executable),
        "CLI_AGENT_CLAUDE_PATH": str(executable), "CLI_AGENT_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "CLI_AGENT_EXTRA_ENV_JSON": "", "CLI_AGENT_CODEX_MODEL": "", "CLI_AGENT_CLAUDE_MODEL": "",
        "CLI_AGENT_CODEX_SANDBOX": "read-only", "CLI_AGENT_CLAUDE_PERMISSION_MODE": "default",
        "CLI_AGENT_CLAUDE_ALLOWED_TOOLS": "",
    }.items():
        monkeypatch.setenv(key, value)
    reload_settings()
    import app.services.cli_agent_service as cli_module

    launches = []
    original_popen = cli_module.subprocess.Popen

    def spawn_only_the_inert_fixture(command, *args, **kwargs):
        assert command[0] == str(executable), "this test must never launch an installed CLI"
        launches.append(list(command))
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(cli_module.subprocess, "Popen", spawn_only_the_inert_fixture)
    register_workspace_user(client, prefix="cli_plan_http", display_name="CLI plan owner")
    conversation = client.post("/api/v1/conversations", json={"title": "CLI plan evidence"})
    assert conversation.status_code == 201, conversation.text
    return {"conversation_id": conversation.json()["conversation_id"], "launches": launches}


def _run(client, boundary, task, *, mode="codex", synchronous=False):
    payload = {
        "task": task, "run_mode": mode, "conversation_id": boundary["conversation_id"],
        "trigger_channel": "companion", "include_memory": False, "include_library": False,
    }
    if synchronous:
        response = client.post("/api/v1/agent/run", json=payload)
        assert response.status_code == 200, response.text
        return response.json()
    admitted = client.post("/api/v1/agent/runs", json=payload)
    assert admitted.status_code == 200, admitted.text
    run_id = admitted.json()["run_id"]
    streamed = client.get(f"/api/v1/agent/runs/{run_id}/stream")
    assert streamed.status_code == 200, streamed.text
    response = client.get(f"/api/v1/agent/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _durable_events(client, run_id):
    response = client.get(f"/api/v1/agent/runs/{run_id}/events?limit=500")
    assert response.status_code == 200, response.text
    assert response.json()["has_more"] is False
    return response.json()["items"]


def _assert_persisted_history_and_replay(client, boundary, run):
    run_id, plan = run["run_id"], run["task_plan"]
    launches = len(boundary["launches"])
    with SessionLocal() as db:
        assert json.loads(db.get(AgentRun, run_id).task_plan_json) == plan
    events = _durable_events(client, run_id)
    plans = [event["payload"]["task_plan"] for event in events if event["event"] == "plan.updated"]
    assert plans and plans[-1] == plan
    assert [value["revision"] for value in plans] == list(range(1, plan["revision"] + 1))
    assert events[-1]["payload"]["task_plan"] == plan
    assert events[-1]["payload"]["status"] == run["status"]
    for _ in range(2):
        replay = client.get(f"/api/v1/agent/runs/{run_id}/stream")
        assert replay.status_code == 200, replay.text
        frames = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        assert [frame["payload"]["task_plan"] for frame in frames if frame["event"] == "plan.updated"] == plans
        assert client.post(f"/api/v1/agent/runs/{run_id}/start").status_code == 200
    history = client.get("/api/v1/chat/history", params={"conversation_id": boundary["conversation_id"]})
    assert history.status_code == 200, history.text
    recorded = next(item["response_payload"] for item in history.json()["items"]
                    if item["response_payload"].get("run_id") == run_id)
    assert recorded["task_plan"] == plan and recorded["status"] == run["status"]
    assert len(boundary["launches"]) == launches, "history and observation cannot spawn the CLI again"
    assert _durable_events(client, run_id) == events
    assert "PRIVATE" not in json.dumps(plans, ensure_ascii=False)
    return plans


@pytest.mark.parametrize("synchronous", [False, True], ids=["stream", "sync"])
def test_codex_native_plan_round_trips_and_next_native_turn_does_not_inherit_it(client, fake_cli_plan_boundary, synchronous):
    boundary = fake_cli_plan_boundary
    run = _run(client, boundary, "CODEX_PLAN", synchronous=synchronous)
    assert run["status"] == "completed", run
    plan = run["task_plan"]
    assert plan["source"] == "codex.todo_list" and plan["model_reported"] is True
    assert plan["revision"] == 2
    assert [step["status"] for step in plan["steps"]] == ["completed", "pending"]
    assert run["execution_receipt"] is None
    plans = _assert_persisted_history_and_replay(client, boundary, run)
    assert [step["status"] for step in plans[0]["steps"]] == ["pending", "pending"]
    next_run = _run(client, boundary, "SECOND_WITHOUT_PLAN", synchronous=synchronous)
    assert next_run["status"] == "completed" and next_run["task_plan"] is None
    assert next_run["context_snapshot"]["continuity"] == "native_resume"
    assert not any(event["event"] == "plan.updated" for event in _durable_events(client, next_run["run_id"]))
    assert client.get(f"/api/v1/agent/runs/{run['run_id']}").json()["task_plan"] == plan
    assert len(boundary["launches"]) == 2


def test_failed_codex_turn_retains_model_completed_plan_without_becoming_successful(client, fake_cli_plan_boundary):
    boundary = fake_cli_plan_boundary
    run = _run(client, boundary, "CODEX_FAILED_CLAIMED_DONE")
    assert run["status"] == "failed", run
    assert run["task_plan"]["model_reported"] is True
    assert all(step["status"] == "completed" for step in run["task_plan"]["steps"])
    _assert_persisted_history_and_replay(client, boundary, run)
    assert len(boundary["launches"]) == 1


def test_claude_structured_tasks_flow_through_subprocess_service_db_and_ignore_failed_update(client, fake_cli_plan_boundary):
    boundary = fake_cli_plan_boundary
    run = _run(client, boundary, "CLAUDE_STRUCTURED_TASKS", mode="claude_code")
    assert run["status"] == "completed", run
    plan = run["task_plan"]
    assert plan["source"] == "claude.tasks" and plan["model_reported"] is True
    assert plan["revision"] == 2
    assert plan["steps"] == [{"id": "s1", "content": "核对结构化资料", "status": "in_progress"}]
    plans = _assert_persisted_history_and_replay(client, boundary, run)
    assert [value["steps"][0]["status"] for value in plans] == ["pending", "in_progress"]
    assert "PRIVATE" not in json.dumps(plan, ensure_ascii=False)
    assert len(boundary["launches"]) == 1
