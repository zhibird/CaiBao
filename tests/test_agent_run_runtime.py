"""Execution lifetime, replay cursors, bounded scheduling and lease recovery."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.core.exceptions import DomainValidationError
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_worker import AgentRunWorker
from app.schemas.agent import AgentRunResponse, AgentStreamEvent
from app.services.agent_run_runtime import AgentRunRuntime, AgentRuntimeBusy


def _event(run_id, name, seq=0, **payload):
    return AgentStreamEvent(run_id=run_id, event=name, seq=seq, payload=payload)


def _response(db, run_id):
    db.expire_all()
    run = db.get(AgentRun, run_id)
    return AgentRunResponse(
        run_id=run_id, team_id=run.team_id, user_id=run.user_id,
        conversation_id=run.conversation_id, space_id=None, task=run.task,
        status=run.status, answer=run.final_answer, dry_run=False, max_steps=5,
        created_at=run.created_at, completed_at=run.completed_at,
    )


def _until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        threading.Event().wait(0.01)
    raise AssertionError("Background condition did not become true.")


@pytest.fixture
def runtime_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    runtimes = []

    class Source:
        def __init__(self, db):
            self.db = db

        def get_run(self, *, run_id, **kwargs):
            return _response(self.db, run_id)

        def stream_run(self, run_id):
            run = self.db.get(AgentRun, run_id)
            run.status = "completed"
            run.final_answer = "Done."
            self.db.commit()
            yield _event(run_id, "run.completed", status="completed", answer="Done.")

    def make(script=None, **options):
        class Scripted(Source):
            def stream_run(self, run_id):
                return script(self, run_id)

        source = Scripted if script else Source
        runtime = AgentRunRuntime(sessions, source, poll_seconds=0.01, **options)
        runtimes.append(runtime)
        return runtime

    def queued(*, status="queued", mode="agent_auto", conversation_id=None, team_id="team", user_id="user"):
        run_id = str(uuid4())
        with sessions() as db:
            db.add(AgentRun(
                run_id=run_id, team_id=team_id, user_id=user_id, status=status,
                task="A durable task", conversation_id=conversation_id,
                request_payload_json=json.dumps({"run_mode": mode}),
            ))
            db.commit()
        return run_id

    yield make, sessions, queued, Source
    for runtime in runtimes:
        runtime.shutdown()
    engine.dispose()


def _worker_state(sessions, run_id):
    with sessions() as db:
        worker = db.get(AgentRunWorker, run_id)
        return worker.state if worker else None


def test_disconnect_does_not_stop_execution_and_replay_never_reexecutes(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    released = threading.Event()
    executing = threading.Event()
    calls = []

    def script(source, run_id):
        calls.append((run_id, threading.current_thread().name))
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        yield _event(run_id, "run.started", seq=0, status="running")
        executing.set()
        assert released.wait(4)
        # Real nested source generators reset seq. The journal must ignore it.
        yield _event(run_id, "llm.delta", seq=0, text="Finished in background.")
        run.status = "completed"
        run.final_answer = "Finished in background."
        source.db.commit()
        yield _event(run_id, "run.completed", seq=1, status="completed")

    runtime = make(script)
    run_id = queued()
    try:
        assert runtime.ensure_started(run_id) is True
        subscriber = runtime.iter_events(run_id)
        first = next(subscriber)
        assert first.event == "run.started"
        subscriber.close()
        assert executing.wait(2)
        assert runtime.ensure_started(run_id) is False
        released.set()
        _until(lambda: _worker_state(sessions, run_id) == "finished")
        events = list(runtime.iter_events(run_id))
        assert [event.seq for event in events] == sorted({event.seq for event in events})
        assert len(events) >= 3
        assert events[-1].payload["answer"] == "Finished in background."
        assert events[-1].payload["run_id"] == run_id
        assert runtime.read_events(run_id, after=first.seq) == events[1:]
        assert runtime.ensure_started(run_id) is False
        assert len(calls) == 1
        assert calls[0][1].startswith("caibao-agent")
    finally:
        released.set()


def test_events_polling_is_read_only_and_cursor_is_scoped_to_run(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    runtime = make()
    first, second = queued(), queued()
    assert runtime.read_events(first) == []
    with sessions() as db:
        assert db.get(AgentRun, first).status == "queued"
        assert db.get(AgentRunWorker, first) is None
    runtime._append(_event(first, "llm.delta", text="one"))
    runtime._append(_event(second, "llm.delta", text="other"))
    runtime._append(_event(first, "llm.delta", text="two"))
    first_page = runtime.read_events(first, limit=1)
    assert len(first_page) == 1
    remaining = runtime.read_events(first, after=first_page[0].seq)
    assert [event.payload["text"] for event in remaining] == ["two"]
    assert list(runtime.read_events(second))[0].payload["text"] == "other"


@pytest.mark.parametrize("during_factory", [False, True])
def test_failure_before_run_started_is_persisted_and_not_left_running(runtime_factory, during_factory):
    make, sessions, queued, _ = runtime_factory

    def broken(source, run_id):
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        raise RuntimeError("route resolution unavailable")
        yield  # Make the failure occur on the first next(), before run.started.

    runtime = make(broken)
    if during_factory:
        def broken_factory(db):
            raise RuntimeError("service initialization unavailable")
        runtime._service_factory = broken_factory
    run_id = queued()
    runtime.ensure_started(run_id)
    _until(lambda: _worker_state(sessions, run_id) == "finished")
    with sessions() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "failed"
        assert run.completed_at is not None
    events = list(runtime.iter_events(run_id))
    assert events[-1].event == "run.failed"
    assert events[-1].payload["status"] == "failed"
    assert "unavailable" in events[-1].payload["error"]
    # A reconnect must not reconstruct the same failed provider graph.
    assert runtime.ensure_started(run_id) is False
    assert runtime.read_events(run_id) == events


def test_heartbeat_keeps_slow_tool_alive_across_other_server_startup(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    entered, release = threading.Event(), threading.Event()

    def slow(source, run_id):
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        entered.set()
        assert release.wait(4)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    active = make(slow, lease_seconds=0.3, heartbeat_seconds=0.04)
    observer = make(lease_seconds=0.3, heartbeat_seconds=0.04)
    run_id = queued()
    try:
        active.ensure_started(run_id)
        assert entered.wait(2)
        # The source produces no events while waiting; only the independent
        # heartbeat can protect it beyond its original lease deadline.
        threading.Event().wait(0.7)
        observer.start()
        assert observer.recover_expired() == 0
        assert observer.ensure_started(run_id) is False
        with sessions() as db:
            assert db.get(AgentRun, run_id).status == "running"
        release.set()
        _until(lambda: _worker_state(sessions, run_id) == "finished")
        assert list(observer.iter_events(run_id))[-1].payload["status"] == "completed"
    finally:
        release.set()


def test_expired_owner_is_failed_once_without_repeating_actions(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    runtime = make()
    run_id = queued(status="running")
    with sessions() as db:
        db.add(AgentRunWorker(
            run_id=run_id, owner_id="dead-process", state="active", slot_key=None,
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        ))
        db.add(AgentRunEvent(run_id=run_id, event="tool.result", payload_json='{"result":"already written"}'))
        db.commit()
    runtime.start()
    assert runtime.recover_expired() == 0
    with sessions() as db:
        assert db.get(AgentRun, run_id).status == "interrupted"
        worker = db.get(AgentRunWorker, run_id)
        assert worker.state == "interrupted"
        assert worker.slot_key is None
    assert runtime.ensure_started(run_id) is False
    failures = [event for event in runtime.read_events(run_id) if event.event == "run.failed"]
    assert "lease expired" in failures[0].payload["error"]
    assert runtime.read_events(run_id)[0].event == "tool.result"
    assert runtime._futures == {}


def test_capacity_is_bounded_and_shutdown_explicitly_fails_queued_jobs(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(source, run_id):
        calls.append(run_id)
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        entered.set()
        assert release.wait(4)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    runtime = make(slow, max_workers=1, max_queue=1)
    first, second, third = queued(), queued(), queued()
    try:
        runtime.ensure_started(first)
        assert entered.wait(2)
        runtime.ensure_started(second)
        with pytest.raises(AgentRuntimeBusy):
            runtime.ensure_started(third)
        with sessions() as db:
            assert db.get(AgentRunWorker, third) is None
            assert db.get(AgentRun, third).status == "queued"
        runtime.shutdown(wait=False)
        _until(lambda: _worker_state(sessions, second) == "finished")
        with sessions() as db:
            assert db.get(AgentRun, second).status == "interrupted"
        assert calls == [first]
        release.set()
        runtime.shutdown()
        assert not runtime._heartbeat.is_alive()
        assert not runtime._owned
    finally:
        release.set()


@pytest.mark.parametrize(("mode", "second_mode"), [
    ("codex", "codex"), (" CODEX ", "agent_auto"), ("Claude_Code", "codex"), (" PI ", "agent_auto"),
    ("agent_auto", "agent_auto"), ("agent_auto", "codex"), ("agent_plan", "pi"),
])
def test_conversation_slot_is_shared_across_servers_and_modes_then_released(runtime_factory, mode, second_mode):
    make, sessions, queued, _ = runtime_factory
    entered, release = threading.Event(), threading.Event()

    def slow(source, run_id):
        entered.set()
        assert release.wait(4)
        run = source.db.get(AgentRun, run_id)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    active, other = make(slow), make()
    first = queued(mode=mode, conversation_id="shared")
    second = queued(mode=second_mode, conversation_id="shared")
    try:
        active.ensure_started(first)
        assert entered.wait(2)
        with pytest.raises(AgentRuntimeBusy, match="conversation"):
            other.ensure_started(second)
        with sessions() as db:
            assert db.get(AgentRun, second).status == "queued"
            assert db.get(AgentRunWorker, second) is None
        release.set()
        _until(lambda: _worker_state(sessions, first) == "finished")
        assert other.ensure_started(second) is True
        _until(lambda: _worker_state(sessions, second) == "finished")
    finally:
        release.set()


@pytest.mark.parametrize("second_mode", ["codex", "agent_auto"])
def test_expired_cli_slot_waits_for_original_owner_source_cleanup(runtime_factory, second_mode):
    make, sessions, queued, _ = runtime_factory
    entered, release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()

    def slow(source, run_id):
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        try:
            yield _event(run_id, "run.started", status="running")
            entered.set()
            assert release.wait(4)
            # The runtime detects lost ownership on this event, then closes
            # the source. Its cleanup must precede releasing the CLI slot.
            yield _event(run_id, "llm.delta", text="Returned after lease expiry.")
        finally:
            cleanup_entered.set()
            assert cleanup_release.wait(4)

    active, other = make(slow), make()
    first = queued(mode="codex", conversation_id="shared")
    second = queued(mode=second_mode, conversation_id="shared")
    try:
        assert active.ensure_started(first)
        assert entered.wait(2)
        # Simulate a suspended server or failed renewal while its CLI remains
        # alive. Another server may interrupt the run, but cannot free its cwd.
        active._stop.set()
        with sessions() as db:
            worker = db.get(AgentRunWorker, first)
            worker.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=30)
            db.commit()
        other.start()
        with sessions() as db:
            assert db.get(AgentRun, first).status == "interrupted"
            worker = db.get(AgentRunWorker, first)
            assert worker.state == "interrupted"
            assert worker.slot_key is not None
        with pytest.raises(AgentRuntimeBusy, match="conversation"):
            other.ensure_started(second)
        # A different owner cannot claim that its own cleanup freed this slot.
        other._finish_claim(first)
        with pytest.raises(AgentRuntimeBusy, match="conversation"):
            other.ensure_started(second)
        release.set()
        assert cleanup_entered.wait(2)
        with pytest.raises(AgentRuntimeBusy, match="conversation"):
            other.ensure_started(second)
        cleanup_release.set()
        _until(lambda: first not in active._futures)
        with sessions() as db:
            worker = db.get(AgentRunWorker, first)
            assert worker.state == "interrupted"
            assert worker.slot_key is None
            assert db.get(AgentRun, first).status == "interrupted"
        assert other.ensure_started(second)
        _until(lambda: _worker_state(sessions, second) == "finished")
    finally:
        release.set()
        cleanup_release.set()


@pytest.mark.parametrize("state", ["interrupted", "finished"])
def test_cli_approval_cannot_reclaim_workspace_until_cleanup_finished(runtime_factory, state):
    make, sessions, queued, _ = runtime_factory
    runtime = make()
    run_id = queued(status="requires_confirmation", mode="codex", conversation_id="shared")
    with sessions() as db:
        run = db.get(AgentRun, run_id)
        db.add(AgentRunWorker(
            run_id=run_id, owner_id="old-cli-owner", state=state,
            slot_key=runtime._conversation_slot_key(run),
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        ))
        db.commit()
    with pytest.raises(AgentRuntimeBusy, match="previous CLI execution"):
        runtime.execute_confirmation(run_id=run_id, team_id="team", user_id="user")
    with sessions() as db:
        assert db.get(AgentRun, run_id).status == "requires_confirmation"
        assert db.get(AgentRunWorker, run_id).owner_id == "old-cli-owner"
    assert runtime._futures == {}


@pytest.mark.parametrize("second_mode", ["codex", "agent_auto"])
def test_cli_source_cleanup_failure_keeps_workspace_reserved(runtime_factory, second_mode):
    make, sessions, queued, _ = runtime_factory

    class BrokenSource:
        def __iter__(self):
            return self

        def __next__(self):
            raise RuntimeError("CLI source failed.")

        def close(self):
            raise RuntimeError("CLI source could not finish cleanup.")

    runtime, other = make(lambda source, run_id: BrokenSource()), make()
    first = queued(mode="codex", conversation_id="shared")
    second = queued(mode=second_mode, conversation_id="shared")
    assert runtime.ensure_started(first)
    _until(lambda: first not in runtime._futures)
    with sessions() as db:
        assert db.get(AgentRun, first).status == "failed"
        assert db.get(AgentRunWorker, first).slot_key is not None
    with pytest.raises(AgentRuntimeBusy, match="conversation"):
        other.ensure_started(second)


def test_api_source_cleanup_failure_does_not_reserve_conversation_forever(runtime_factory):
    make, sessions, queued, _ = runtime_factory

    class BrokenSource:
        def __iter__(self):
            return self

        def __next__(self):
            raise RuntimeError("API source failed.")

        def close(self):
            raise RuntimeError("API source cleanup failed.")

    runtime, other = make(lambda source, run_id: BrokenSource()), make()
    first = queued(conversation_id="shared")
    second = queued(mode="codex", conversation_id="shared")
    assert runtime.ensure_started(first)
    _until(lambda: first not in runtime._futures)
    with sessions() as db:
        assert db.get(AgentRun, first).status == "failed"
        assert db.get(AgentRunWorker, first).slot_key is None
    assert other.ensure_started(second)
    _until(lambda: _worker_state(sessions, second) == "finished")


def test_simultaneous_api_and_cli_claims_have_exactly_one_conversation_owner(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    barrier = threading.Barrier(3)
    entered, release = threading.Event(), threading.Event()
    executed = []

    def execution(source, run_id):
        executed.append(run_id)
        entered.set()
        assert release.wait(4)
        run = source.db.get(AgentRun, run_id)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    api, cli = make(execution), make(execution)
    first, second = queued(conversation_id="shared"), queued(mode="codex", conversation_id="shared")

    def claim(runtime, run_id):
        barrier.wait(timeout=2)
        try:
            return runtime.ensure_started(run_id)
        except AgentRuntimeBusy:
            return "busy"

    try:
        with ThreadPoolExecutor(max_workers=2) as racers:
            claims = [racers.submit(claim, api, first), racers.submit(claim, cli, second)]
            barrier.wait(timeout=2)
            results = [claim.result(timeout=2) for claim in claims]
        assert results.count(True) == 1
        assert results.count("busy") == 1
        assert entered.wait(2)
        assert len(executed) == 1
        with sessions() as db:
            workers = [db.get(AgentRunWorker, run_id) for run_id in (first, second)]
            assert sum(worker is not None for worker in workers) == 1
            assert next(worker for worker in workers if worker is not None).slot_key is not None
    finally:
        release.set()


@pytest.mark.parametrize("new_mode", ["agent_auto", "codex"])
def test_expired_api_releases_slot_and_late_cleanup_cannot_unlock_new_owner(runtime_factory, new_mode):
    make, sessions, queued, _ = runtime_factory
    old_entered, old_release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()

    def stale_api(source, run_id):
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        try:
            yield _event(run_id, "run.started", status="running")
            old_entered.set()
            assert old_release.wait(4)
            yield _event(run_id, "llm.delta", text="Stale API output must be fenced.")
        finally:
            cleanup_entered.set()
            assert cleanup_release.wait(4)

    def new_execution(source, run_id):
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        new_entered.set()
        assert new_release.wait(4)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    old, other = make(stale_api), make(new_execution)
    first = queued(conversation_id="shared")
    second = queued(mode=new_mode, conversation_id="shared")
    third = queued(conversation_id="shared")
    try:
        assert old.ensure_started(first)
        assert old_entered.wait(2)
        old._stop.set()
        with sessions() as db:
            worker = db.get(AgentRunWorker, first)
            worker.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=30)
            db.commit()
        other.start()
        with sessions() as db:
            assert db.get(AgentRun, first).status == "interrupted"
            assert db.get(AgentRunWorker, first).slot_key is None
        assert other.ensure_started(second)
        assert new_entered.wait(2)
        old_release.set()
        assert cleanup_entered.wait(2)
        cleanup_release.set()
        _until(lambda: first not in old._futures)
        assert all(event.event != "llm.delta" for event in old.read_events(first))
        with sessions() as db:
            assert db.get(AgentRunWorker, second).state == "active"
            assert db.get(AgentRunWorker, second).slot_key is not None
        with pytest.raises(AgentRuntimeBusy, match="Another agent task"):
            old.ensure_started(third)
    finally:
        old_release.set()
        cleanup_release.set()
        new_release.set()


@pytest.mark.parametrize(("confirmation_mode", "active_mode"), [
    ("codex", "agent_auto"), ("agent_auto", "codex"), ("agent_auto", "agent_auto"),
])
def test_confirmation_cannot_reclaim_conversation_occupied_by_another_mode(runtime_factory, confirmation_mode, active_mode):
    make, sessions, queued, Source = runtime_factory
    entered, release = threading.Event(), threading.Event()
    confirmed = []

    def active_execution(source, run_id):
        entered.set()
        assert release.wait(4)
        run = source.db.get(AgentRun, run_id)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    class Confirming(Source):
        def confirm_run(self, *, run_id, **kwargs):
            confirmed.append(run_id)
            run = self.db.get(AgentRun, run_id)
            run.status = "completed"
            self.db.commit()
            return _response(self.db, run_id)

    confirming, active = make(), make(active_execution)
    confirming._service_factory = Confirming
    pending = queued(status="requires_confirmation", mode=confirmation_mode, conversation_id="shared")
    busy = queued(mode=active_mode, conversation_id="shared")
    with sessions() as db:
        db.add(AgentRunWorker(run_id=pending, owner_id="previous-owner", state="finished", slot_key=None,
                             lease_expires_at=datetime.now(timezone.utc)))
        db.commit()
    try:
        assert active.ensure_started(busy)
        assert entered.wait(2)
        with pytest.raises(AgentRuntimeBusy, match="Another agent task"):
            confirming.execute_confirmation(run_id=pending, team_id="team", user_id="user")
        assert confirmed == []
        with sessions() as db:
            assert db.get(AgentRun, pending).status == "requires_confirmation"
            assert db.get(AgentRunWorker, pending).owner_id == "previous-owner"
        release.set()
        _until(lambda: _worker_state(sessions, busy) == "finished")
        assert confirming.execute_confirmation(run_id=pending, team_id="team", user_id="user").status == "completed"
        assert confirmed == [pending]
    finally:
        release.set()


@pytest.mark.parametrize("separate_scope", ["no-conversation", "different-conversation", "different-user", "different-team"])
def test_conversation_slot_does_not_serialize_unrelated_runs(runtime_factory, separate_scope):
    make, sessions, queued, _ = runtime_factory
    entered, release = threading.Event(), threading.Event()

    def active_execution(source, run_id):
        entered.set()
        assert release.wait(4)
        run = source.db.get(AgentRun, run_id)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    active, other = make(active_execution), make()
    first = queued(conversation_id=None if separate_scope == "no-conversation" else "shared")
    second = queued(
        mode="codex", conversation_id=None if separate_scope == "no-conversation" else (
            "different" if separate_scope == "different-conversation" else "shared"
        ),
        user_id="other-user" if separate_scope == "different-user" else "user",
        team_id="other-team" if separate_scope == "different-team" else "team",
    )
    try:
        assert active.ensure_started(first)
        assert entered.wait(2)
        assert other.ensure_started(second)
        _until(lambda: _worker_state(sessions, second) == "finished")
    finally:
        release.set()


def test_confirmation_reuses_worker_lease_and_preserves_invalid_approval(runtime_factory):
    make, sessions, queued, Source = runtime_factory
    runtime = make()
    run_id = queued(status="requires_confirmation")
    tokens = []

    class Confirming(Source):
        def confirm_run(self, *, run_id, approval_token, **kwargs):
            tokens.append(approval_token)
            if approval_token != "correct":
                raise DomainValidationError("Approval is stale.")
            run = self.db.get(AgentRun, run_id)
            run.status = "completed"
            run.final_answer = "Approved action completed."
            self.db.commit()
            return _response(self.db, run_id)

    runtime._service_factory = Confirming
    with pytest.raises(DomainValidationError, match="stale"):
        runtime.execute_confirmation(run_id=run_id, team_id="team", user_id="user", approval_token="stale")
    _until(lambda: _worker_state(sessions, run_id) == "finished")
    with sessions() as db:
        assert db.get(AgentRun, run_id).status == "requires_confirmation"
    response = runtime.execute_confirmation(run_id=run_id, team_id="team", user_id="user", approval_token="correct")
    assert response.status == "completed"
    assert runtime.read_events(run_id)[-1].payload["answer"] == "Approved action completed."
    assert tokens == ["stale", "correct"]


def test_paused_approval_remains_usable_after_owner_expires(runtime_factory):
    make, sessions, queued, Source = runtime_factory
    runtime = make()
    run_id = queued(status="requires_confirmation")
    with sessions() as db:
        db.add(AgentRunWorker(
            run_id=run_id, owner_id="dead-owner", state="active",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=20),
        ))
        db.commit()

    class Confirming(Source):
        def confirm_run(self, *, run_id, **kwargs):
            run = self.db.get(AgentRun, run_id)
            run.status = "completed"
            self.db.commit()
            return _response(self.db, run_id)

    runtime._service_factory = Confirming
    runtime.start()
    with sessions() as db:
        assert db.get(AgentRun, run_id).status == "requires_confirmation"
        assert db.get(AgentRunWorker, run_id).state == "interrupted"
    assert runtime.execute_confirmation(run_id=run_id, team_id="team", user_id="user").status == "completed"


def test_approval_failure_after_claim_is_persisted_as_terminal(runtime_factory):
    make, sessions, queued, Source = runtime_factory
    runtime = make()
    run_id = queued(status="requires_confirmation")

    class FailingConfirmation(Source):
        def confirm_run(self, *, run_id, **kwargs):
            run = self.db.get(AgentRun, run_id)
            run.status = "running"
            self.db.commit()
            raise RuntimeError("Tool setup failed.")

    runtime._service_factory = FailingConfirmation
    with pytest.raises(RuntimeError, match="Tool setup"):
        runtime.execute_confirmation(run_id=run_id, team_id="team", user_id="user")
    with sessions() as db:
        assert db.get(AgentRun, run_id).status == "failed"
    assert runtime.read_events(run_id)[-1].payload["task"] == "A durable task"


def test_unmanaged_running_run_is_reported_without_adoption_or_hanging(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    runtime = make()
    run_id = queued(status="running")
    assert runtime.ensure_started(run_id) is False
    events = list(runtime.iter_events(run_id))
    assert events[-1].event == "run.in_progress"
    assert events[-1].payload["status"] == "running"
    with sessions() as db:
        assert db.get(AgentRunWorker, run_id) is None


def test_quiet_tool_sends_transport_heartbeats_without_creating_events(runtime_factory):
    make, sessions, queued, _ = runtime_factory
    release, entered = threading.Event(), threading.Event()

    def quiet(source, run_id):
        run = source.db.get(AgentRun, run_id)
        run.status = "running"
        source.db.commit()
        entered.set()
        assert release.wait(4)
        run.status = "completed"
        source.db.commit()
        yield _event(run_id, "run.completed", status="completed")

    runtime = make(quiet)
    run_id = queued()
    try:
        runtime.ensure_started(run_id)
        assert entered.wait(2)
        subscriber = runtime.iter_events(run_id, heartbeat_seconds=0.03)
        assert next(subscriber) is None
        assert runtime.read_events(run_id) == []
        subscriber.close()
    finally:
        release.set()


def test_background_dependency_graph_uses_real_services_on_the_same_session(runtime_factory, monkeypatch, tmp_path):
    from fastapi.params import Depends
    from app.api import deps
    from app.core.config import reload_settings

    _, sessions, _, _ = runtime_factory
    monkeypatch.setenv("UPLOAD_ROOT_DIR", str(tmp_path / "uploads"))
    reload_settings()
    monkeypatch.setattr(deps, "_get_consolidation_service", lambda **kwargs: None)
    monkeypatch.setattr(deps, "get_plugin_manager", lambda: None)
    monkeypatch.setattr(deps, "get_event_bus", lambda: None)
    monkeypatch.setattr(deps, "_register_mcp_handlers", lambda *args: None)
    with sessions() as db:
        service = deps.create_background_agent_service(db)
        for candidate in (
            service, service.user_service, service.document_service, service.tool_service,
            service.chat_history_service, service.llm_model_service,
            service.rag_chat_service.memory_service, service.rag_chat_service.retrieval_service,
        ):
            assert candidate.db is db
            assert not any(isinstance(value, Depends) for value in vars(candidate).values())
