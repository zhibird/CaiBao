"""Fenced local CLI checkpoints, separate from CaiBao's conversation history.

A native session ID is evidence that a CLI created a session, not evidence that
it finished a turn. Only ``commit_success`` promotes that evidence and the
official history IDs consumed by the successful turn into a resumable checkpoint.
No prompts, model responses, environment values, or credentials are persisted.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence

from app.core.config import Settings
from app.core.exceptions import DomainValidationError

SESSION_FILE_NAME = ".caibao_cli_sessions.json"
_STATE_VERSION = 2
_RUNNERS = {"claude_code", "codex", "pi"}
_THREAD_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
_THREAD_LOCKS_GUARD = threading.Lock()
_SAFE_ENV_SUFFIXES = ("BASE_URL", "ENDPOINT", "API_URL", "MODEL", "PROVIDER")
_SECRET_KEY = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.I)
_MAX_CHECKPOINT_IDS = 512


def build_cli_session_binding(
    settings: Settings, runner_name: str, *, dry_run: bool = False,
    system_prompt: str = "",
) -> str:
    """Fingerprint the runner's declared connection/model/command configuration.

    Secret environment entries are deliberately excluded: rotating an API key
    must not save its value (even inside a configuration snapshot). CLI-owned
    configuration outside CaiBao remains opaque; this binding makes no claims
    about changes to a CLI's external account or config files.
    """
    _validate_runner(runner_name)
    fields = {
        "claude_code": (
            "cli_agent_claude_path", "cli_agent_claude_model",
            "cli_agent_claude_permission_mode", "cli_agent_claude_allowed_tools",
        ),
        "codex": ("cli_agent_codex_path", "cli_agent_codex_model", "cli_agent_codex_sandbox"),
        "pi": (
            "cli_agent_pi_path", "cli_agent_pi_provider", "cli_agent_pi_model",
            "cli_agent_pi_thinking",
        ),
    }[runner_name]
    raw = (settings.cli_agent_extra_env_json or "").strip()
    try:
        extra_env = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise DomainValidationError("CLI_AGENT_EXTRA_ENV_JSON 不是合法 JSON。") from exc
    if not isinstance(extra_env, dict):
        raise DomainValidationError("CLI_AGENT_EXTRA_ENV_JSON 必须是 JSON 对象。")
    declared_connection = {
        str(key): str(value)
        for key, value in extra_env.items()
        if not _SECRET_KEY.search(str(key))
        and str(key).upper().endswith(_SAFE_ENV_SUFFIXES)
    }
    config = {
        "binding_version": 1, "runner": runner_name, "dry_run": bool(dry_run),
        "command_config": {name: getattr(settings, name) for name in fields},
        "declared_connection": declared_connection,
        "role_prompt_hash": hashlib.sha256(system_prompt.strip().encode("utf-8")).hexdigest(),
    }
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CliSessionState:
    resume_session_id: str | None = None
    consumed_message_ids: tuple[str, ...] = ()
    native_run_ids: tuple[str, ...] = ()
    history_boundary: str | None = None
    fresh_reason: str | None = "new"
    uncertain: bool = False


@dataclass
class CliSessionDispatch:
    runner_name: str
    binding: str
    run_id: str
    attempt_id: str
    resume_session_id: str | None
    consumed_message_ids: tuple[str, ...]
    native_run_ids: tuple[str, ...]
    history_boundary: str | None
    fresh_reason: str | None
    uncertain: bool
    _store: "CliSessionStore" = field(repr=False)
    _lease: BinaryIO = field(repr=False)
    _finished: bool = field(default=False, repr=False)

    def close(self) -> None:
        """An uncommitted dispatch is uncertain, including generator cancellation."""
        if not self._finished:
            self._store.mark_uncertain(self)

    def __enter__(self) -> "CliSessionDispatch":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class CliSessionStore:
    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.path = self.workdir / SESSION_FILE_NAME

    def inspect(self, runner_name: str, binding: str) -> CliSessionState:
        _validate_runner(runner_name)
        _validate_binding(binding)
        with self._state_lock():
            return self._project(self._read(), runner_name, binding)

    def begin_dispatch(
        self, runner_name: str, binding: str, *, run_id: str,
        message_ids: Sequence[str] = (), history_boundary: str | None = None,
    ) -> CliSessionDispatch:
        """Persist inflight before sending, holding a nonblocking runner lease.

        A stale inflight record after a crash is never resumed. Another live
        process/thread holding the lease rejects admission instead of dispatching
        a competing turn. The caller must close the returned dispatch in finally.
        """
        _validate_runner(runner_name)
        _validate_binding(binding)
        _identifier(run_id, "run_id")
        staged_ids = _message_ids(message_ids)
        if history_boundary is not None:
            _identifier(history_boundary, "history_boundary")
        lease = _open_lock(self.workdir / f".caibao_cli_{runner_name}.lock")
        try:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DomainValidationError("这个会话的 CLI 正在执行，请等待当前任务结束。") from exc
            attempt_id = str(uuid.uuid4())
            with self._state_lock():
                data = self._read()
                state = self._project(data, runner_name, binding)
                runners = _runners(data)
                previous = runners.get(runner_name)
                previous = previous if isinstance(previous, dict) else {}
                attempted_run_ids = _message_ids(previous.get("attempted_run_ids", ()))
                if run_id in attempted_run_ids:
                    raise DomainValidationError("这个任务已派发过，结果不明时请新建任务，不自动重试。")
                # Keep the last committed position as evidence, even after a
                # failed attempt. Projection fences it off from future resume.
                runners[runner_name] = {
                    "binding": binding, "status": "inflight",
                    "checkpoint": previous.get("checkpoint"),
                    "attempted_run_ids": list(_bounded_ids((*attempted_run_ids, run_id))),
                    "pending": {
                        "attempt_id": attempt_id, "run_id": run_id,
                        "session_id": None,
                        "message_ids": list(staged_ids),
                        "history_boundary": history_boundary,
                    },
                }
                self._write(data)
            return CliSessionDispatch(
                runner_name=runner_name, binding=binding, run_id=run_id,
                attempt_id=attempt_id, resume_session_id=state.resume_session_id,
                consumed_message_ids=state.consumed_message_ids,
                native_run_ids=state.native_run_ids,
                history_boundary=state.history_boundary, fresh_reason=state.fresh_reason,
                uncertain=state.uncertain, _store=self, _lease=lease,
            )
        except BaseException:
            lease.close()
            raise

    def note_session(self, dispatch: CliSessionDispatch, session_id: str) -> None:
        """Save pending evidence without promoting the successful checkpoint."""
        _identifier(session_id, "session_id", max_length=4096)
        with self._state_lock():
            data = self._read()
            entry = self._owned_entry(data, dispatch)
            entry["pending"]["session_id"] = session_id
            self._write(data)

    def commit_success(
        self, dispatch: CliSessionDispatch, *, session_id: str | None = None,
        message_ids: Sequence[str] | None = None, history_boundary: str | None = None,
    ) -> bool:
        """Promote only an explicitly successful turn with native session evidence.

        IDs describe official messages actually supplied/observed by this turn,
        not a timestamp guess. Missing native evidence forces a fresh next turn.
        Returns False in that case, leaving the previous checkpoint untouched.
        """
        if session_id is not None:
            _identifier(session_id, "session_id", max_length=4096)
        ids = _message_ids(message_ids) if message_ids is not None else None
        if history_boundary is not None:
            _identifier(history_boundary, "history_boundary")
        try:
            with self._state_lock():
                data = self._read()
                entry = self._owned_entry(data, dispatch)
                pending = entry["pending"]
                evidence = session_id or pending.get("session_id")
                if not evidence:
                    entry["status"] = "uncertain"
                    self._write(data)
                    return False
                _identifier(evidence, "session_id", max_length=4096)
                supplied_ids = ids if ids is not None else _message_ids(pending.get("message_ids", ()))
                checkpoint_ids = _bounded_ids((*dispatch.consumed_message_ids, *supplied_ids))
                native_run_ids = _bounded_ids((*dispatch.native_run_ids, dispatch.run_id))
                entry["checkpoint"] = {
                    "binding": dispatch.binding,
                    "session_id": evidence,
                    "consumed_message_ids": list(checkpoint_ids),
                    "native_run_ids": list(native_run_ids),
                    "history_boundary": history_boundary if history_boundary is not None else pending.get("history_boundary"),
                }
                entry["status"] = "ready"
                entry["pending"] = None
                # Compatibility mirror for old integrations. The typed reader
                # only trusts the validated checkpoint, never this bare string.
                data[dispatch.runner_name] = evidence
                self._write(data)
                return True
        finally:
            self._release(dispatch)

    def mark_uncertain(self, dispatch: CliSessionDispatch) -> None:
        """Fence ambiguous/failed turns; retain pending evidence and old cursor."""
        if dispatch._finished:
            return
        try:
            with self._state_lock():
                data = self._read()
                entry = self._owned_entry(data, dispatch)
                entry["status"] = "uncertain"
                self._write(data)
        finally:
            self._release(dispatch)

    def _owned_entry(self, data: dict, dispatch: CliSessionDispatch) -> dict:
        if dispatch._store is not self or dispatch._finished:
            raise DomainValidationError("CLI 执行状态已结束，不能再次提交。")
        entry = _runners(data).get(dispatch.runner_name)
        pending = entry.get("pending") if isinstance(entry, dict) else None
        if (not isinstance(pending, dict)
                or entry.get("status") != "inflight"
                or entry.get("binding") != dispatch.binding
                or pending.get("attempt_id") != dispatch.attempt_id):
            raise DomainValidationError("CLI 执行状态已变化，不能覆盖当前会话。")
        return entry

    @staticmethod
    def _release(dispatch: CliSessionDispatch) -> None:
        dispatch._finished = True
        dispatch._lease.close()

    @staticmethod
    def _project(data: dict, runner_name: str, binding: str) -> CliSessionState:
        entry = _runners(data).get(runner_name)
        if not isinstance(entry, dict):
            return CliSessionState(fresh_reason="legacy" if data.get(runner_name) else "new")
        if data.get("version") != _STATE_VERSION:
            return CliSessionState(fresh_reason="uncertain", uncertain=True)
        if entry.get("status") != "ready":
            return CliSessionState(fresh_reason="uncertain", uncertain=True)
        checkpoint = entry.get("checkpoint")
        if (entry.get("binding") != binding or not isinstance(checkpoint, dict)
                or checkpoint.get("binding") != binding):
            return CliSessionState(fresh_reason="binding_changed")
        try:
            session_id = _identifier(checkpoint.get("session_id"), "session_id", max_length=4096)
            ids = _message_ids(checkpoint.get("consumed_message_ids", ()))
            run_ids = _message_ids(checkpoint.get("native_run_ids", ()))
            boundary = checkpoint.get("history_boundary")
            if boundary is not None:
                _identifier(boundary, "history_boundary")
        except DomainValidationError:
            return CliSessionState(fresh_reason="uncertain", uncertain=True)
        return CliSessionState(
            resume_session_id=session_id, consumed_message_ids=ids,
            native_run_ids=run_ids, history_boundary=boundary, fresh_reason=None,
        )

    @contextlib.contextmanager
    def _state_lock(self) -> Iterator[None]:
        key = str(self.path.resolve())
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        with thread_lock, _open_lock(self.workdir / ".caibao_cli_sessions.lock") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        data["version"] = _STATE_VERSION
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.workdir,
                prefix=".caibao_cli_sessions-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = handle.name
                os.chmod(temporary, 0o600)
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            with contextlib.suppress(OSError):
                directory_fd = os.open(self.workdir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)


def load_legacy_cli_session_id(workdir: Path, runner_name: str) -> str | None:
    """Compatibility accessor; this bare value is never a precise checkpoint."""
    store = CliSessionStore(workdir)
    _validate_runner(runner_name)
    with store._state_lock():
        data = store._read()
        entry = _runners(data).get(runner_name)
        if isinstance(entry, dict):
            if entry.get("status") != "ready" or data.get("version") != _STATE_VERSION:
                return None
            checkpoint = entry.get("checkpoint")
            if not isinstance(checkpoint, dict) or checkpoint.get("binding") != entry.get("binding"):
                return None
            value = checkpoint.get("session_id")
            return value if isinstance(value, str) and value else None
        value = data.get(runner_name)
        return value if isinstance(value, str) and value else None


def save_legacy_cli_session_id(workdir: Path, runner_name: str, session_id: str) -> None:
    """Save legacy evidence atomically, invalidating any precise checkpoint."""
    store = CliSessionStore(workdir)
    _validate_runner(runner_name)
    _identifier(session_id, "session_id", max_length=4096)
    with store._state_lock():
        data = store._read()
        data[runner_name] = session_id
        entry = _runners(data).get(runner_name)
        if isinstance(entry, dict):
            entry["status"] = "uncertain"
        store._write(data)


def _runners(data: dict) -> dict:
    if not isinstance(data.get("runners"), dict):
        data["runners"] = {}
    return data["runners"]


def _open_lock(path: Path) -> BinaryIO:
    return os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a+b")


def _validate_runner(runner_name: str) -> None:
    if runner_name not in _RUNNERS:
        raise DomainValidationError("未知的 CLI session 类型。")


def _validate_binding(binding: str) -> None:
    if not isinstance(binding, str) or not re.fullmatch(r"v1:[a-f0-9]{64}", binding):
        raise DomainValidationError("CLI 会话配置指纹无效。")


def _identifier(value: object, name: str, *, max_length: int = 128) -> str:
    if (not isinstance(value, str) or not value or len(value) > max_length
            or any(ord(char) < 32 for char in value)):
        raise DomainValidationError(f"CLI {name} 无效。")
    return value


def _message_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (tuple, list)):
        raise DomainValidationError("CLI history message IDs 必须是列表。")
    return _bounded_ids(_identifier(value, "message_id") for value in values)


def _bounded_ids(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))[-_MAX_CHECKPOINT_IDS:]
