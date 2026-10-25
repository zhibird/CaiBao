"""Native session evidence must never outrun an officially successful turn."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import DomainValidationError
from app.services.cli_session_store import (
    CliSessionStore,
    build_cli_session_binding,
    load_legacy_cli_session_id,
    save_legacy_cli_session_id,
)


def _settings(**changes) -> Settings:
    values = dict(
        cli_agent_pi_path="pi", cli_agent_pi_model="model-a",
        cli_agent_pi_provider="provider-a", cli_agent_extra_env_json="",
    )
    values.update(changes)
    return Settings(_env_file=None, **values)


def _binding(runner="pi", *, prompt="role", **changes) -> str:
    values = dict(
        cli_agent_pi_path="pi", cli_agent_pi_model="model-a",
        cli_agent_pi_provider="provider-a", cli_agent_extra_env_json="",
    )
    values.update(changes)
    settings = Settings(_env_file=None, **values)
    return build_cli_session_binding(settings, runner, system_prompt=prompt)


def _checkpoint(store, *, binding=None, run_id="run-one", session_id="session-one", ids=("message-one",)):
    dispatch = store.begin_dispatch("pi", binding or _binding(), run_id=run_id)
    store.note_session(dispatch, session_id)
    assert store.commit_success(dispatch, message_ids=ids, history_boundary=ids[-1] if ids else None)


def _child_script(tmp_path: Path, code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), _binding()],
        text=True, capture_output=True, check=False, env=env, timeout=15,
    )


def test_legacy_string_is_readable_but_never_a_precise_checkpoint(tmp_path):
    save_legacy_cli_session_id(tmp_path, "pi", "legacy-session")
    assert load_legacy_cli_session_id(tmp_path, "pi") == "legacy-session"
    store = CliSessionStore(tmp_path)
    state = store.inspect("pi", _binding())
    assert state.resume_session_id is None
    assert state.fresh_reason == "legacy"
    with store.begin_dispatch("pi", _binding(), run_id="run-one") as dispatch:
        assert dispatch.resume_session_id is None
        assert dispatch.consumed_message_ids == ()
        store.note_session(dispatch, "new-session")
        assert store.commit_success(dispatch, message_ids=["message-one"])
    state = store.inspect("pi", _binding())
    assert state.resume_session_id == "new-session"
    assert state.consumed_message_ids == ("message-one",)
    assert state.native_run_ids == ("run-one",)


def test_partial_session_is_pending_evidence_not_success(tmp_path):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    with store.begin_dispatch("pi", _binding(), run_id="run-two") as dispatch:
        assert dispatch.resume_session_id == "session-one"
        store.note_session(dispatch, "pending-session")
        raw = json.loads(store.path.read_text())
        assert raw["runners"]["pi"]["checkpoint"]["session_id"] == "session-one"
        assert raw["runners"]["pi"]["pending"]["session_id"] == "pending-session"
        assert load_legacy_cli_session_id(tmp_path, "pi") is None
    raw = json.loads(store.path.read_text())
    assert raw["runners"]["pi"]["checkpoint"]["consumed_message_ids"] == ["message-one"]
    assert raw["runners"]["pi"]["status"] == "uncertain"
    with store.begin_dispatch("pi", _binding(), run_id="run-three") as recovered:
        assert recovered.resume_session_id is None
        assert recovered.consumed_message_ids == ()
        assert recovered.native_run_ids == ()
        assert recovered.uncertain
        store.note_session(recovered, "fresh-session")
        assert store.commit_success(recovered, message_ids=["message-one", "message-two"])
    assert store.inspect("pi", _binding()).native_run_ids == ("run-three",)


def test_success_without_current_session_evidence_does_not_advance_position(tmp_path):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    dispatch = store.begin_dispatch("pi", _binding(), run_id="run-two")
    assert dispatch.resume_session_id == "session-one"
    assert not store.commit_success(dispatch, message_ids=["message-two"])
    raw = json.loads(store.path.read_text())
    assert raw["runners"]["pi"]["checkpoint"]["consumed_message_ids"] == ["message-one"]
    assert store.inspect("pi", _binding()).uncertain


def test_success_merges_official_ids_and_native_runs_but_binding_change_starts_fresh(tmp_path):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    with store.begin_dispatch("pi", _binding(), run_id="run-two", message_ids=["message-two"]) as dispatch:
        assert dispatch.native_run_ids == ("run-one",)
        store.note_session(dispatch, "session-one")
        assert store.commit_success(dispatch)
    state = store.inspect("pi", _binding())
    assert state.consumed_message_ids == ("message-one", "message-two")
    assert state.native_run_ids == ("run-one", "run-two")
    changed = _binding(cli_agent_pi_model="model-b")
    with store.begin_dispatch("pi", changed, run_id="run-three") as dispatch:
        assert dispatch.fresh_reason == "binding_changed"
        assert dispatch.resume_session_id is None
        assert dispatch.consumed_message_ids == ()
        assert dispatch.native_run_ids == ()
        assert store.commit_success(dispatch, session_id="different-session", message_ids=["message-three"])
    state = store.inspect("pi", changed)
    assert state.consumed_message_ids == ("message-three",)
    assert state.native_run_ids == ("run-three",)


def test_checkpoint_ids_are_bounded_and_deduplicated(tmp_path):
    store = CliSessionStore(tmp_path)
    with store.begin_dispatch("pi", _binding(), run_id="run-one") as dispatch:
        ids = [f"message-{index}" for index in range(800)] + ["message-799"]
        assert store.commit_success(dispatch, session_id="session-one", message_ids=ids)
    state = store.inspect("pi", _binding())
    assert len(state.consumed_message_ids) == 512
    assert state.consumed_message_ids[0] == "message-288"
    assert state.consumed_message_ids[-1] == "message-799"
    # Simulate a long-lived valid checkpoint; the next promotion trims run IDs.
    raw = json.loads(store.path.read_text())
    raw["runners"]["pi"]["checkpoint"]["native_run_ids"] = [f"run-{i}" for i in range(800)]
    store.path.write_text(json.dumps(raw))
    with store.begin_dispatch("pi", _binding(), run_id="run-latest") as dispatch:
        assert store.commit_success(dispatch, session_id="session-one")
    assert len(store.inspect("pi", _binding()).native_run_ids) == 512
    assert store.inspect("pi", _binding()).native_run_ids[-1] == "run-latest"


def test_same_runner_has_exclusive_thread_and_process_lease(tmp_path):
    store = CliSessionStore(tmp_path)
    dispatch = store.begin_dispatch("pi", _binding(), run_id="run-one")
    try:
        def competing_thread():
            other = CliSessionStore(tmp_path)
            with pytest.raises(DomainValidationError, match="正在执行"):
                other.begin_dispatch("pi", _binding(), run_id="run-two")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(competing_thread).result(timeout=5)
        child = _child_script(tmp_path, """
import sys
from pathlib import Path
from app.core.exceptions import DomainValidationError
from app.services.cli_session_store import CliSessionStore
try:
    CliSessionStore(Path(sys.argv[1])).begin_dispatch('pi', sys.argv[2], run_id='child')
except DomainValidationError:
    sys.exit(0)
sys.exit(9)
""")
        assert child.returncode == 0, child.stderr
    finally:
        dispatch.close()
    with CliSessionStore(tmp_path).begin_dispatch("pi", _binding(), run_id="next") as next_dispatch:
        assert next_dispatch.uncertain


def test_different_runners_write_concurrently_without_losing_entries(tmp_path):
    store = CliSessionStore(tmp_path)
    def run(runner):
        binding = _binding(runner)
        other = CliSessionStore(tmp_path)
        with other.begin_dispatch(runner, binding, run_id=f"run-{runner}") as dispatch:
            assert other.commit_success(dispatch, session_id=f"session-{runner}")
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(run, ["pi", "codex", "claude_code"]))
    raw = json.loads(store.path.read_text())
    assert set(raw["runners"]) == {"pi", "codex", "claude_code"}
    assert all(entry["status"] == "ready" for entry in raw["runners"].values())


def test_process_death_releases_lease_but_never_resumes_inflight(tmp_path):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    child = _child_script(tmp_path, """
import os, sys
from pathlib import Path
from app.services.cli_session_store import CliSessionStore
store = CliSessionStore(Path(sys.argv[1]))
dispatch = store.begin_dispatch('pi', sys.argv[2], run_id='crashed-run')
store.note_session(dispatch, 'session-before-crash')
os._exit(0)
""")
    assert child.returncode == 0, child.stderr
    with store.begin_dispatch("pi", _binding(), run_id="new-task") as dispatch:
        assert dispatch.resume_session_id is None
        assert dispatch.native_run_ids == ()
        assert dispatch.consumed_message_ids == ()
        assert dispatch.uncertain
        raw = json.loads(store.path.read_text())
        assert raw["runners"]["pi"]["checkpoint"]["consumed_message_ids"] == ["message-one"]


def test_atomic_failure_never_exposes_partial_json_or_advances_cursor(tmp_path, monkeypatch):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    dispatch = store.begin_dispatch("pi", _binding(), run_id="run-two")
    store.note_session(dispatch, "session-two")
    before = store.path.read_bytes()
    with monkeypatch.context() as patch:
        def fail_replace(*_args):
            raise OSError("disk replacement failed")
        patch.setattr("app.services.cli_session_store.os.replace", fail_replace)
        with pytest.raises(OSError, match="replacement failed"):
            store.commit_success(dispatch, message_ids=["message-two"])
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob(".caibao_cli_sessions-*.tmp"))
    with store.begin_dispatch("pi", _binding(), run_id="run-three") as next_dispatch:
        assert next_dispatch.uncertain
        assert next_dispatch.consumed_message_ids == ()


def test_failed_initial_write_prevents_dispatch_and_releases_lease(tmp_path, monkeypatch):
    store = CliSessionStore(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(store, "_write", lambda _data: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            store.begin_dispatch("pi", _binding(), run_id="run-one")
    with store.begin_dispatch("pi", _binding(), run_id="run-two") as dispatch:
        assert dispatch.fresh_reason == "new"


def test_stale_attempt_cannot_overwrite_another_checkpoint(tmp_path):
    store = CliSessionStore(tmp_path)
    stale = store.begin_dispatch("pi", _binding(), run_id="run-one")
    stale.close()
    with store.begin_dispatch("pi", _binding(), run_id="run-two") as dispatch:
        store.note_session(dispatch, "current-session")
        with pytest.raises(DomainValidationError, match="已结束"):
            store.commit_success(stale, session_id="stale-session")
        assert store.commit_success(dispatch, message_ids=["message-two"])
    assert store.inspect("pi", _binding()).resume_session_id == "current-session"


def test_legacy_overwrite_invalidates_precise_checkpoint(tmp_path):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    save_legacy_cli_session_id(tmp_path, "pi", "unverified-session")
    assert store.inspect("pi", _binding()).uncertain
    assert load_legacy_cli_session_id(tmp_path, "pi") is None


def test_typed_legacy_accessor_uses_committed_evidence_not_stale_mirror(tmp_path):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    raw = json.loads(store.path.read_text())
    raw["pi"] = "stale-mirror"
    store.path.write_text(json.dumps(raw))
    assert load_legacy_cli_session_id(tmp_path, "pi") == "session-one"


@pytest.mark.parametrize("succeeded", [True, False])
def test_same_run_id_is_never_automatically_dispatched_again(tmp_path, succeeded):
    store = CliSessionStore(tmp_path)
    with store.begin_dispatch("pi", _binding(), run_id="run-one") as dispatch:
        store.note_session(dispatch, "native-session")
        if succeeded:
            assert store.commit_success(dispatch)
    with pytest.raises(DomainValidationError, match="已派发过"):
        store.begin_dispatch("pi", _binding(), run_id="run-one")
    # A new user task can acquire the released lease.
    with store.begin_dispatch("pi", _binding(), run_id="run-two") as dispatch:
        assert dispatch.uncertain is not succeeded


def test_unrecognized_future_state_version_is_not_claimed_as_precise_resume(tmp_path):
    store = CliSessionStore(tmp_path)
    _checkpoint(store)
    raw = json.loads(store.path.read_text())
    raw["version"] = 999
    store.path.write_text(json.dumps(raw))
    state = store.inspect("pi", _binding())
    assert state.resume_session_id is None
    assert state.uncertain


def test_binding_changes_for_role_model_connection_permissions_and_command():
    original = _binding()
    assert original != _binding(prompt="new role")
    assert original != _binding(cli_agent_pi_model="model-b")
    assert original != _binding(cli_agent_pi_provider="provider-b")
    assert original != _binding(cli_agent_pi_path="/custom/pi")
    settings = _settings()
    assert original != build_cli_session_binding(settings, "pi", system_prompt="role", dry_run=True)
    assert _binding(cli_agent_extra_env_json='{"OPENAI_BASE_URL":"https://one.example"}') != _binding(
        cli_agent_extra_env_json='{"OPENAI_BASE_URL":"https://two.example"}',
    )
    assert _binding("codex", cli_agent_codex_sandbox="read-only") != _binding("codex", cli_agent_codex_sandbox="workspace-write")


def test_secrets_never_enter_fingerprint_material_or_state(tmp_path):
    first = _binding(cli_agent_extra_env_json='{"OPENAI_API_KEY":"secret-one","ACCESS_TOKEN":"token-one"}')
    second = _binding(cli_agent_extra_env_json='{"OPENAI_API_KEY":"secret-two","ACCESS_TOKEN":"token-two"}')
    assert first == second
    prompt = "private-role-instructions"
    store = CliSessionStore(tmp_path)
    binding = _binding(prompt=prompt, cli_agent_extra_env_json='{"OPENAI_API_KEY":"do-not-persist"}')
    with store.begin_dispatch("pi", binding, run_id="run-one") as dispatch:
        assert store.commit_success(dispatch, session_id="native-session", message_ids=["message-one"])
    text = store.path.read_text()
    assert prompt not in text
    assert "do-not-persist" not in text
    assert "OPENAI_API_KEY" not in text
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("content", ["{", "[]", '{"pi":{"bad":"type"}}'])
def test_corrupt_or_wrong_legacy_shape_cannot_resume(tmp_path, content):
    path = tmp_path / ".caibao_cli_sessions.json"
    path.write_text(content)
    state = CliSessionStore(tmp_path).inspect("pi", _binding())
    assert state.resume_session_id is None
    assert state.consumed_message_ids == ()


@pytest.mark.parametrize("kwargs", [{"message_ids": "raw prompt"}, {"message_ids": ["bad\nID"]}, {"history_boundary": "bad\nID"}])
def test_invalid_metadata_does_not_create_inflight(tmp_path, kwargs):
    store = CliSessionStore(tmp_path)
    with pytest.raises(DomainValidationError):
        store.begin_dispatch("pi", _binding(), run_id="run-one", **kwargs)
    assert not store.path.exists()
