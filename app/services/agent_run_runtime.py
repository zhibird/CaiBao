"""Bounded background execution with durable, reconnectable event delivery.

HTTP streams only read the journal. A worker owns its SQLAlchemy session and
consumes the source independently of subscribers. Leases are renewed by one
separate thread, including while a model or tool is blocked. A lost lease is
reported as failure; side effects are never retried automatically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import threading
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_worker import AgentRunWorker
from app.models.agent_step import AgentStep
from app.schemas.agent import AgentExecutionReceipt, AgentRunResponse, AgentStepResponse, AgentStreamEvent
from app.schemas.task_plan import AgentTaskPlan
from app.services.cli_agent_service import CLI_AGENT_MODES

logger = logging.getLogger(__name__)
_ACTIVE = {"queued", "running"}


class AgentRuntimeBusy(DomainValidationError):
    """The caller may retry dispatch of the same queued run later."""


class _LeaseLost(RuntimeError):
    pass


class AgentRunRuntime:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        service_factory: Callable[[Session], Any],
        *,
        max_workers: int = 4,
        max_queue: int = 16,
        lease_seconds: float = 60,
        heartbeat_seconds: float = 10,
        poll_seconds: float = 0.1,
    ) -> None:
        if max_workers < 1 or max_queue < 0:
            raise ValueError("Worker count must be positive and queue capacity nonnegative.")
        if not 0 < heartbeat_seconds < lease_seconds or poll_seconds <= 0:
            raise ValueError("Heartbeat must be positive and shorter than the lease.")
        self._session_factory = session_factory
        self._service_factory = service_factory
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds
        self._max_workers = max_workers
        self._owner_id = str(uuid4())
        self._slots = threading.BoundedSemaphore(max_workers + max_queue)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        self._heartbeat: threading.Thread | None = None
        self._futures: dict[str, Future] = {}
        self._owned: set[str] = set()
        self._closing = False

    def start(self) -> None:
        with self._lock:
            if self._closing:
                raise RuntimeError("Agent runtime has shut down.")
            if self._executor is not None:
                return
            self.recover_expired()
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="caibao-agent",
            )
            self._heartbeat = threading.Thread(
                target=self._maintain_leases, name="caibao-agent-leases", daemon=False,
            )
            self._heartbeat.start()

    def ensure_started(self, run_id: str) -> bool:
        """Dispatch once. Reopening a stream never reruns an existing claim."""
        return self._submit(run_id, confirmation=None) is not None

    def execute_confirmation(
        self, *, run_id: str, team_id: str, user_id: str, approval_token: str | None = None,
    ) -> AgentRunResponse:
        """Keep the synchronous API while the confirmed work belongs to a worker."""
        future = self._submit(run_id, confirmation=(team_id, user_id, approval_token))
        if future is None:
            raise DomainValidationError("This confirmation is already being processed.")
        # Cancelling the HTTP subscriber cannot cancel this future.
        try:
            return future.result()
        finally:
            with self._changed:
                while self._futures.get(run_id) is future:
                    self._changed.wait(timeout=self._poll_seconds)

    def _submit(self, run_id: str, confirmation: tuple[str, str, str | None] | None) -> Future | None:
        self.start()
        with self._lock:
            if self._closing:
                raise RuntimeError("Agent runtime has shut down.")
            if run_id in self._futures:
                return None
            with self._session_factory() as db:
                run = db.get(AgentRun, run_id)
                if run is None:
                    raise EntityNotFoundError(f"Agent run '{run_id}' not found.")
                worker = db.get(AgentRunWorker, run_id)
                if confirmation:
                    if (run.team_id, run.user_id) != confirmation[:2]:
                        raise EntityNotFoundError(f"Agent run '{run_id}' not found.")
                    if run.status != "requires_confirmation":
                        raise DomainValidationError("Only runs waiting for confirmation can be confirmed.")
                    if (
                        worker is not None and worker.state != "active"
                        and worker.slot_key is not None and self._is_cli_run(run)
                    ):
                        raise AgentRuntimeBusy(
                            "The previous CLI execution has not finished closing; use a new conversation or wait for it to exit."
                        )
                elif worker is not None:
                    # In particular, replay must work when service setup itself
                    # failed earlier. Do not construct its broken graph again.
                    return None
                elif run.status != "queued":
                    # Legacy completed runs have no journal yet; materialize a
                    # read-only snapshot, never run their source again.
                    has_events = db.scalar(select(AgentRunEvent.seq).where(
                        AgentRunEvent.run_id == run_id,
                    ).limit(1))
                    if has_events is None:
                        self._snapshot_from_service(db, run)
                    return None
                if not self._slots.acquire(blocking=False):
                    raise AgentRuntimeBusy("Agent workers are busy; retry this queued run shortly.")
                try:
                    if worker is not None:
                        if not confirmation or worker.state == "active":
                            self._slots.release()
                            return None
                        # The initial worker may have finished while waiting
                        # for human approval. Reacquire exactly that run.
                        claimed = db.execute(
                            update(AgentRunWorker).where(
                                AgentRunWorker.run_id == run_id,
                                AgentRunWorker.state.in_({"finished", "interrupted"}),
                            ).values(
                                owner_id=self._owner_id, state="active",
                                slot_key=self._conversation_slot_key(run), lease_expires_at=self._deadline(),
                            )
                        ).rowcount
                        if not claimed:
                            self._slots.release()
                            return None
                    else:
                        db.add(AgentRunWorker(
                            run_id=run_id, owner_id=self._owner_id, state="active",
                            slot_key=self._conversation_slot_key(run), lease_expires_at=self._deadline(),
                        ))
                    db.commit()
                except IntegrityError as exc:
                    db.rollback()
                    self._slots.release()
                    # A simultaneous request/process can claim the same run.
                    existing = db.get(AgentRunWorker, run_id)
                    if existing is not None:
                        # A confirmation already has a finished worker row. Its
                        # existence alone does not prove that another caller
                        # claimed this run: another run may own the shared
                        # conversation slot (including in a different mode).
                        db.refresh(existing)
                        db.refresh(run)
                        if not confirmation or existing.state == "active" or run.status != "requires_confirmation":
                            return None
                    raise AgentRuntimeBusy(
                        "Another agent task is using this conversation; retry this run after it finishes."
                    ) from exc
                except BaseException:
                    self._slots.release()
                    raise
            self._owned.add(run_id)
            try:
                future = self._executor.submit(self._execute, run_id, confirmation)
            except BaseException:
                self._owned.discard(run_id)
                self._slots.release()
                self._fail_owned(run_id, "Agent worker could not be scheduled.")
                raise
            self._futures[run_id] = future
            future.add_done_callback(lambda done: self._job_done(run_id, done))
            return future

    @staticmethod
    def _is_cli_run(run: AgentRun) -> bool:
        try:
            mode = str(json.loads(run.request_payload_json).get("run_mode", "agent_auto")).strip().lower()
        except (TypeError, ValueError, AttributeError):
            return False
        return mode in CLI_AGENT_MODES

    @staticmethod
    def _conversation_slot_key(run: AgentRun) -> str | None:
        if not run.conversation_id:
            return None
        scope = json.dumps([run.team_id, run.user_id, run.conversation_id])
        return hashlib.sha256(scope.encode()).hexdigest()

    def _execute(self, run_id: str, confirmation: tuple[str, str, str | None] | None) -> AgentRunResponse | None:
        source = None
        with self._session_factory() as db:
            try:
                service = self._service_factory(db)
                if confirmation:
                    response = service.confirm_run(
                        run_id=run_id, team_id=confirmation[0], user_id=confirmation[1],
                        approval_token=confirmation[2],
                    )
                    self.record_snapshot(response)
                else:
                    source = service.stream_run(run_id)
                    for event in source:
                        self._append(event, require_owner=True)
                    db.expire_all()
                    run = db.get(AgentRun, run_id)
                    if run is None:
                        raise EntityNotFoundError(f"Agent run '{run_id}' not found.")
                    if run.status in _ACTIVE:
                        raise RuntimeError("Agent execution ended without a final status.")
                    response = self._snapshot_from_service(db, run, service=service)
                return response
            except _LeaseLost:
                db.rollback()
                logger.warning("Agent worker lost ownership of run %s", run_id)
                self._enforce_interruption(run_id)
                return None
            except Exception as exc:
                db.rollback()
                rejected = (
                    confirmation is not None and isinstance(exc, DomainValidationError)
                    and db.scalar(select(AgentRun.status).where(AgentRun.run_id == run_id)) == "requires_confirmation"
                )
                if not rejected:
                    logger.exception("Background agent run %s failed", run_id)
                    self._fail_owned(run_id, f"Agent execution failed: {exc}")
                # Confirmation callers still receive an explicit HTTP failure;
                # the full failure state is also available through the journal.
                if confirmation:
                    raise
                return None
            finally:
                if source is not None and hasattr(source, "close"):
                    source.close()
                self._finish_claim(run_id)

    def _job_done(self, run_id: str, future: Future) -> None:
        try:
            if future.cancelled():
                self._fail_owned(run_id, "Agent execution interrupted before starting: server shutdown.", status="interrupted")
                self._finish_claim(run_id)
            elif future.exception() is not None:
                # This also catches session construction or finalization errors
                # outside the source generator's exception guard. A failing
                # source.close() must not release an uncertain CLI workspace.
                self._fail_owned(run_id, "Agent worker could not finish execution.")
                self._finish_claim(run_id, release_cli_slot=False)
        except Exception:
            # An unavailable DB must not strand a capacity slot or renew an
            # owner which no longer has a worker. Recovery can expire it later.
            logger.exception("Could not finalize agent worker %s", run_id)
        finally:
            with self._changed:
                self._futures.pop(run_id, None)
                self._owned.discard(run_id)
                self._slots.release()
                if self._closing and not self._owned:
                    self._stop.set()
                self._changed.notify_all()

    def _append(self, event: AgentStreamEvent, *, require_owner: bool = False) -> AgentStreamEvent:
        with self._session_factory() as db:
            if require_owner:
                # This write locks the claim until the event commits. Recovery
                # cannot interleave a stale event after losing ownership.
                owned = db.execute(
                    update(AgentRunWorker).where(
                        AgentRunWorker.run_id == event.run_id,
                        AgentRunWorker.owner_id == self._owner_id,
                        AgentRunWorker.state == "active",
                    ).values(lease_expires_at=self._deadline())
                ).rowcount
                if not owned:
                    raise _LeaseLost(event.run_id)
            # Serializing journal commits per run matters even with a global
            # database sequence: PostgreSQL allocates IDs before commit. Without
            # this lock, a cancel snapshot could commit a higher cursor before
            # an older delta and a reconnect would then skip that delta.
            exists = db.execute(update(AgentRun).where(
                AgentRun.run_id == event.run_id,
            ).values(status=AgentRun.status)).rowcount
            if not exists:
                raise EntityNotFoundError(f"Agent run '{event.run_id}' not found.")
            record = AgentRunEvent(
                run_id=event.run_id, event=event.event, step_index=event.step_index,
                payload_json=json.dumps(event.payload, ensure_ascii=False, default=str),
            )
            db.add(record)
            db.flush()
            persisted = event.model_copy(update={"seq": record.seq})
            db.commit()
        self._notify()
        return persisted

    def record_snapshot(self, response: AgentRunResponse) -> None:
        payload = response.model_dump(mode="json")
        name = {
            "cancelled": "run.cancelled", "failed": "run.failed",
            "interrupted": "run.failed", "queued": "run.in_progress", "running": "run.in_progress",
        }.get(response.status, "run.completed")
        with self._session_factory() as db:
            last = db.scalar(select(AgentRunEvent).where(
                AgentRunEvent.run_id == response.run_id,
            ).order_by(AgentRunEvent.seq.desc()).limit(1))
            if last is not None and last.event == name and json.loads(last.payload_json) == payload:
                return
        self._append(AgentStreamEvent(event=name, run_id=response.run_id, payload=payload))

    def _snapshot_from_service(self, db: Session, run: AgentRun, *, service: Any = None) -> AgentRunResponse:
        service = service if service is not None else self._service_factory(db)
        response = service.get_run(
            run_id=run.run_id, team_id=run.team_id, user_id=run.user_id,
        )
        self.record_snapshot(response)
        return response

    def read_events(self, run_id: str, after: int = 0, limit: int = 200) -> list[AgentStreamEvent]:
        """Read only; this method never schedules or resumes an action."""
        with self._session_factory() as db:
            records = db.scalars(select(AgentRunEvent).where(
                AgentRunEvent.run_id == run_id, AgentRunEvent.seq > max(0, after),
            ).order_by(AgentRunEvent.seq).limit(max(1, min(limit, 1000)))).all()
            return [AgentStreamEvent(
                event=record.event, run_id=record.run_id, seq=record.seq,
                step_index=record.step_index, payload=json.loads(record.payload_json),
            ) for record in records]

    def iter_events(
        self, run_id: str, after: int = 0, *, heartbeat_seconds: float | None = None,
    ) -> Iterator[AgentStreamEvent | None]:
        if heartbeat_seconds is not None and heartbeat_seconds <= 0:
            raise ValueError("Stream heartbeat interval must be positive.")
        cursor = max(0, after)
        last_delivery = time.monotonic()
        while True:
            events = self.read_events(run_id, after=cursor)
            for event in events:
                cursor = event.seq
                last_delivery = time.monotonic()
                yield event
            if events:
                continue
            with self._session_factory() as db:
                run = db.get(AgentRun, run_id)
                worker = db.get(AgentRunWorker, run_id)
                if run is None:
                    return
                # Synchronous/legacy work has no durable ownership. Report its
                # snapshot without adopting it or waiting forever for a worker.
                done = worker is None or (run.status not in _ACTIVE and worker.state != "active")
            if done or self._stop.is_set():
                # A worker can commit its last event between our read and the
                # completion check; read once more before ending this stream.
                tail = self.read_events(run_id, after=cursor)
                for event in tail:
                    cursor = event.seq
                    yield event
                return
            with self._changed:
                self._changed.wait(timeout=self._poll_seconds)
            if heartbeat_seconds is not None and time.monotonic() - last_delivery >= heartbeat_seconds:
                last_delivery = time.monotonic()
                # Transport liveness comments have no journal cursor and do not
                # turn a quiet tool into a synthetic agent action.
                yield None

    def _fail_owned(self, run_id: str, message: str, *, status: str = "failed") -> None:
        with self._session_factory() as db:
            worker = db.get(AgentRunWorker, run_id)
            if worker is None or worker.owner_id != self._owner_id or worker.state != "active":
                return
            self._mark_failed(db, run_id, message, status=status)
            db.commit()
        self._notify()

    @staticmethod
    def _mark_failed(db: Session, run_id: str, message: str, *, status: str = "failed") -> None:
        changed = db.execute(update(AgentRun).where(
            AgentRun.run_id == run_id, AgentRun.status.in_(_ACTIVE),
        ).values(
            status=status, final_answer=message[:4000], completed_at=datetime.now(timezone.utc),
            required_confirmations_json="[]", loop_state_json=None,
        )).rowcount
        if changed:
            run = db.get(AgentRun, run_id)
            db.refresh(run)
            steps = list(db.scalars(select(AgentStep).where(
                AgentStep.run_id == run_id,
            ).order_by(AgentStep.step_index)))
            response = AgentRunResponse(
                run_id=run_id, team_id=run.team_id, user_id=run.user_id,
                client_request_id=run.client_request_id,
                execution_identity=json.loads(run.execution_identity_json) if run.execution_identity_json else None,
                context_snapshot=json.loads(run.context_snapshot_json) if run.context_snapshot_json else None,
                execution_receipt=AgentExecutionReceipt.from_snapshot(run.execution_receipt_json, run_status=run.status),
                task_plan=AgentTaskPlan.from_snapshot(run.task_plan_json),
                conversation_id=run.conversation_id, space_id=run.space_id,
                app_id=run.app_id, app_version=run.app_version, trigger_channel=run.trigger_channel,
                task=run.task, status=run.status, answer=run.final_answer, model=run.model,
                dry_run=run.dry_run, max_steps=run.max_steps, latency_ms=run.latency_ms,
                created_at=run.created_at, completed_at=run.completed_at,
                steps=[AgentStepResponse.from_record(step) for step in steps],
            )
            payload = response.model_dump(mode="json")
            payload["error"] = message[:4000]
            db.add(AgentRunEvent(run_id=run_id, event="run.failed", payload_json=json.dumps(payload, ensure_ascii=False)))

    def _finish_claim(self, run_id: str, *, release_cli_slot: bool = True) -> None:
        with self._session_factory() as db:
            run = db.get(AgentRun, run_id)
            # API calls have no child CLI workspace to drain. Their state/event
            # fences prevent stale continuations after interruption, so a
            # failed source cleanup must not lock the conversation forever.
            release_slot = release_cli_slot or run is None or not self._is_cli_run(run)
            values: dict[str, Any] = {"state": "finished"}
            if release_slot:
                values["slot_key"] = None
            finished = db.execute(update(AgentRunWorker).where(
                AgentRunWorker.run_id == run_id, AgentRunWorker.owner_id == self._owner_id,
                AgentRunWorker.state == "active",
            ).values(**values)).rowcount
            if finished:
                # Even if the first error write failed transiently, never seal
                # a worker while leaving its run permanently queued/running.
                self._mark_failed(db, run_id, "Agent worker ended before recording a final state.")
            elif release_slot:
                # Lease expiry does not prove that the old CLI process exited.
                # Only that owner, after its source closes, may free the shared
                # conversation workspace. A permanently lost owner leaves the
                # slot reserved rather than risking concurrent CLI sessions.
                db.execute(update(AgentRunWorker).where(
                    AgentRunWorker.run_id == run_id, AgentRunWorker.owner_id == self._owner_id,
                    AgentRunWorker.state == "interrupted",
                ).values(slot_key=None))
            db.commit()
        self._notify()

    def recover_expired(self) -> int:
        """Only expired durable claims are interrupted; live servers are untouched."""
        now = datetime.now(timezone.utc)
        count = 0
        with self._session_factory() as db:
            expired = list(db.scalars(select(AgentRunWorker.run_id).where(
                AgentRunWorker.state == "active", AgentRunWorker.lease_expires_at < now,
            )))
            db.rollback()
            for run_id in expired:
                run = db.get(AgentRun, run_id)
                values: dict[str, Any] = {"state": "interrupted"}
                if run is None or not self._is_cli_run(run):
                    # An API worker can be fenced without owning a persistent
                    # external CLI process/workspace. Let a new task acquire the
                    # conversation while the old owner observes interruption.
                    values["slot_key"] = None
                changed = db.execute(update(AgentRunWorker).where(
                    AgentRunWorker.run_id == run_id, AgentRunWorker.state == "active",
                    AgentRunWorker.lease_expires_at < now,
                ).values(**values)).rowcount
                if changed:
                    self._mark_failed(
                        db, run_id,
                        "Agent execution interrupted: its server lease expired. Review completed actions before starting another run.",
                        status="interrupted",
                    )
                    count += 1
                db.commit()
        if count:
            self._notify()
        return count

    def _enforce_interruption(self, run_id: str) -> None:
        # A suspended process may wake after its lease expired and its tool
        # returned. Its result is not a valid successful continuation.
        with self._session_factory() as db:
            worker = db.get(AgentRunWorker, run_id)
            if worker and worker.state == "interrupted":
                db.execute(update(AgentRun).where(
                    AgentRun.run_id == run_id, AgentRun.status != "cancelled",
                ).values(status="interrupted", completed_at=datetime.now(timezone.utc)))
                db.commit()

    def _maintain_leases(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                with self._lock:
                    owned = tuple(self._owned)
                if owned:
                    with self._session_factory() as db:
                        db.execute(update(AgentRunWorker).where(
                            AgentRunWorker.run_id.in_(owned), AgentRunWorker.owner_id == self._owner_id,
                            AgentRunWorker.state == "active",
                        ).values(lease_expires_at=self._deadline()))
                        db.commit()
                self.recover_expired()
            except Exception:
                # Temporary DB outages do not kill renewal permanently. Another
                # server may eventually expire our claim; no action is retried.
                logger.exception("Could not maintain agent worker leases")

    def shutdown(self, wait: bool = True) -> None:
        """Drain running work, explicitly fail queued jobs, and stop renewal.

        With wait=False, bounded running workers retain renewal until they exit;
        Python cannot safely interrupt a synchronous tool mid-call.
        """
        with self._changed:
            self._closing = True
            executor = self._executor
            if not self._owned:
                self._stop.set()
            self._changed.notify_all()
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
        if wait:
            self._stop.set()
            if self._heartbeat is not None:
                self._heartbeat.join()
        self._notify()

    def _deadline(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self._lease_seconds)

    def _notify(self) -> None:
        with self._changed:
            self._changed.notify_all()
