"""HTTP observation/reconnection never repeats admitted work or crosses identities."""

import json
import time
from uuid import uuid4

from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.services.llm_service import LLMService, LLMStreamChunk
from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


def _admit(client):
    team_id, user_id = register_workspace_user(client, prefix='journal', display_name='Journal owner')
    conversation = client.post('/api/v1/conversations', json={'title': 'Durable journal'}).json()
    response = client.post('/api/v1/companion/runs', json={
        'client_request_id': str(uuid4()), 'task': 'Say hello', 'mode': 'chat',
        'include_memory': False, 'conversation_id': conversation['conversation_id'],
    })
    assert response.status_code == 201
    return team_id, user_id, conversation['conversation_id'], response.json()['run_id']


def _wait_finished(client, run_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = client.get(f'/api/v1/agent/runs/{run_id}').json()
        if run['status'] not in {'queued', 'running'}:
            return run
        time.sleep(.02)
    raise AssertionError('Background task did not finish')


def test_start_without_observer_finishes_and_cursor_replay_never_reexecutes(client, monkeypatch):
    _, _, conversation_id, run_id = _admit(client)
    calls = []

    def model(self, **kwargs):
        calls.append(kwargs)
        yield LLMStreamChunk(delta_text='你好，')
        yield LLMStreamChunk(delta_text='彩宝。', finish_reason='stop')

    monkeypatch.setattr(LLMService, 'stream_chat', model)
    url = f'/api/v1/agent/runs/{run_id}'
    assert client.get(url + '/events').json() == {
        'items': [], 'next_seq': 0, 'status': 'queued', 'has_more': False,
    }
    assert client.get(url).json()['status'] == 'queued'
    assert client.post(url + '/start').status_code == 200
    finished = _wait_finished(client, run_id)
    assert finished['status'] == 'completed'
    assert finished['answer'] == '你好，彩宝。'
    page = client.get(url + '/events?limit=2').json()
    assert len(page['items']) == 2
    assert page['has_more'] is True
    first_cursor = page['next_seq']
    tail = client.get(url + f'/events?after={first_cursor}&limit=500').json()
    assert all(event['seq'] > first_cursor for event in tail['items'])
    sequences = [event['seq'] for event in page['items'] + tail['items']]
    assert sequences == sorted(set(sequences))
    replay = client.get(url + '/stream', headers={'Last-Event-ID': str(first_cursor)})
    assert replay.status_code == 200
    frames = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith('data: ')]
    assert frames and all(item['seq'] > first_cursor for item in frames)
    assert 'id: ' in replay.text
    assert len(calls) == 1
    assert client.post(url + '/start').status_code == 200
    assert len(calls) == 1
    listed = client.get('/api/v1/agent/runs', params={
        'conversation_id': conversation_id, 'trigger_channel': 'companion',
    }).json()['items']
    assert [item['run_id'] for item in listed] == [run_id]
    assert listed[0]['answer'] == '你好，彩宝。'


def test_invalid_observation_cursor_never_starts_work(client):
    _, _, _, run_id = _admit(client)
    url = f'/api/v1/agent/runs/{run_id}'
    for value in ('banana', '-1', str(2**64)):
        assert client.get(url + '/stream', headers={'Last-Event-ID': value}).status_code == 400
    assert client.get(url + '/events?after=-1').status_code == 422
    assert client.get(url).json()['status'] == 'queued'


def test_journal_and_task_discovery_enforce_owner_even_with_shared_team(client):
    team_id, _, conversation_id, run_id = _admit(client)
    register_workspace_user_in_team(client, team_id=team_id, prefix='journal_other', display_name='Other')
    url = f'/api/v1/agent/runs/{run_id}'
    for suffix in ('', '/events', '/stream'):
        assert client.get(url + suffix).status_code == 404
    assert client.post(url + '/start').status_code == 404
    assert client.get('/api/v1/agent/runs', params={'conversation_id': conversation_id}).status_code == 404
    assert client.get('/api/v1/agent/runs').json()['items'] == []
    with SessionLocal() as db:
        assert db.get(AgentRun, run_id).status == 'queued'
    client.cookies.clear()
    assert client.get(url + '/events').status_code == 401
