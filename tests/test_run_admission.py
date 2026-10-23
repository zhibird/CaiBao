"""Retries identify one user intent instead of creating duplicate agent work."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


def _register(client):
    return register_workspace_user(
        client, prefix="admission", display_name="Task admission",
    )


def _request(companion=False, **changes):
    payload = {
        "task": "请简单打个招呼。",
        "client_request_id": uuid4().hex,
        "include_memory": False,
    }
    if companion:
        payload["mode"] = "chat"
    else:
        payload.update(include_library=False, enabled_tools=[])
    return {**payload, **changes}


@pytest.mark.parametrize("companion", [False, True], ids=["agent", "companion"])
def test_retried_submission_returns_same_run_and_request_identity(client, companion):
    team_id, user_id = _register(client)
    endpoint = "/api/v1/companion/runs" if companion else "/api/v1/agent/runs"
    payload = _request(companion)
    first = client.post(endpoint, json=payload)
    second = client.post(endpoint, json=payload)
    assert first.status_code in {200, 201}
    assert second.status_code in {200, 201}
    assert first.json()["run_id"] == second.json()["run_id"]
    run = client.get(f"/api/v1/agent/runs/{first.json()['run_id']}").json()
    assert run["client_request_id"] == payload["client_request_id"]
    with SessionLocal() as db:
        records = db.scalars(select(AgentRun).where(
            AgentRun.team_id == team_id, AgentRun.user_id == user_id,
            AgentRun.client_request_id == payload["client_request_id"],
        )).all()
        assert len(records) == 1


@pytest.mark.parametrize("companion", [False, True], ids=["agent", "companion"])
def test_retried_completed_submission_does_not_start_new_work(client, companion):
    _register(client)
    endpoint = "/api/v1/companion/runs" if companion else "/api/v1/agent/runs"
    payload = _request(companion)
    first = client.post(endpoint, json=payload)
    assert first.status_code in {200, 201}
    run_id = first.json()["run_id"]
    streamed = client.get(f"/api/v1{first.json()['stream_url']}")
    assert streamed.status_code == 200
    finished = client.get(f"/api/v1/agent/runs/{run_id}").json()
    assert finished["status"] == "completed"
    replay = client.post(endpoint, json=payload)
    assert replay.status_code in {200, 201}
    assert replay.json()["run_id"] == run_id
    assert client.get(f"/api/v1/agent/runs/{run_id}").json()["status"] == "completed"


@pytest.mark.parametrize("companion", [False, True], ids=["agent", "companion"])
@pytest.mark.parametrize("change", [
    {"task": "这是另一件事。"},
    {"include_memory": True},
    {"model": "a-different-model"},
], ids=["different-task", "different-memory-policy", "different-model"])
def test_reused_identity_with_different_request_is_rejected(client, companion, change):
    _register(client)
    endpoint = "/api/v1/companion/runs" if companion else "/api/v1/agent/runs"
    payload = _request(companion)
    original = client.post(endpoint, json=payload)
    assert original.status_code in {200, 201}
    conflict = client.post(endpoint, json={**payload, **change})
    assert conflict.status_code == 400
    unchanged = client.get(f"/api/v1/agent/runs/{original.json()['run_id']}").json()
    assert unchanged["task"] == payload["task"]


def test_same_request_identity_is_private_to_each_user(client):
    team_id, first_user = _register(client)
    payload = _request()
    first = client.post("/api/v1/agent/runs", json=payload)
    assert first.status_code == 200
    _, second_user = register_workspace_user_in_team(
        client, team_id=team_id, prefix="admission_peer", display_name="Other task owner",
    )
    second = client.post("/api/v1/agent/runs", json=payload)
    assert second.status_code == 200
    assert first.json()["run_id"] != second.json()["run_id"]
    assert client.get(f"/api/v1/agent/runs/{first.json()['run_id']}").status_code == 404
    with SessionLocal() as db:
        records = db.scalars(select(AgentRun).where(
            AgentRun.team_id == team_id,
            AgentRun.client_request_id == payload["client_request_id"],
        )).all()
        assert {record.user_id for record in records} == {first_user, second_user}


def test_concurrent_duplicate_submissions_admit_only_one_run(client):
    team_id, user_id = _register(client)
    payload = _request()
    ready = Barrier(4)

    def submit():
        ready.wait(timeout=10)
        return client.post("/api/v1/agent/runs", json=payload)

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: submit(), range(4)))
    assert [response.status_code for response in responses] == [200] * 4
    assert len({response.json()["run_id"] for response in responses}) == 1
    with SessionLocal() as db:
        records = db.scalars(select(AgentRun).where(
            AgentRun.team_id == team_id, AgentRun.user_id == user_id,
            AgentRun.client_request_id == payload["client_request_id"],
        )).all()
        assert len(records) == 1


def test_legacy_submissions_without_identity_remain_distinct(client):
    _register(client)
    payload = _request()
    payload.pop("client_request_id")
    first = client.post("/api/v1/agent/runs", json=payload)
    second = client.post("/api/v1/agent/runs", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["run_id"] != second.json()["run_id"]


@pytest.mark.parametrize("companion", [False, True], ids=["agent", "companion"])
@pytest.mark.parametrize("identity", ["", "x" * 65], ids=["empty", "oversized"])
def test_client_request_identity_has_bounded_nonempty_shape(client, companion, identity):
    _register(client)
    endpoint = "/api/v1/companion/runs" if companion else "/api/v1/agent/runs"
    response = client.post(endpoint, json=_request(companion, client_request_id=identity))
    assert response.status_code == 422
