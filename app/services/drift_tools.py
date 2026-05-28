from __future__ import annotations

import contextvars
from typing import Any

from app.core.config import get_settings

# Per-run state via contextvars — safe for concurrent drift invocations
def _make_drift_state() -> dict[str, Any]:
    return {"message_pushed": False, "finished": False, "step_count": 0}

_drift_state_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "drift_state",
)


def new_drift_run(run_token: str) -> str:
    _drift_state_ctx.set({"message_pushed": False, "finished": False, "step_count": 0, "run_token": run_token})
    return run_token


def _active_state() -> dict[str, Any]:
    """Return the current invocation's state dict (creates a fresh one if absent)."""
    try:
        return _drift_state_ctx.get()
    except LookupError:
        st = _make_drift_state()
        _drift_state_ctx.set(st)
        return st


def reset_drift_state(run_token: str = "") -> None:
    new_drift_run(run_token or "default")


def get_drift_state() -> dict[str, Any]:
    return _active_state()


def drift_read_work_file(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
    store,  # DriftStateStore
    skill_name: str,
) -> dict[str, object]:
    filename = str(arguments.get("filename", "notes.md"))
    content = store.read_work_file(team_id, user_id, skill_name, filename)
    return {"filename": filename, "content": content, "exists": bool(content)}


def drift_write_work_file(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
    store,
    skill_name: str,
) -> dict[str, object]:
    filename = str(arguments["filename"])
    content = str(arguments["content"])
    store.write_work_file(team_id, user_id, skill_name, filename, content)
    return {"filename": filename, "written_chars": len(content)}


def drift_edit_work_file(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
    store,
    skill_name: str,
) -> dict[str, object]:
    filename = str(arguments["filename"])
    old_text = str(arguments["old_text"])
    new_text = str(arguments["new_text"])
    replace_all = bool(arguments.get("replace_all", False))
    current = store.read_work_file(team_id, user_id, skill_name, filename)
    if replace_all:
        count = current.count(old_text)
        modified = current.replace(old_text, new_text)
    else:
        count = 1 if old_text in current else 0
        modified = current.replace(old_text, new_text, 1)
    store.write_work_file(team_id, user_id, skill_name, filename, modified)
    return {"filename": filename, "replacements": count}


def drift_list_work_files(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
    store,
    skill_name: str,
) -> dict[str, object]:
    files = store.list_work_files(team_id, user_id, skill_name)
    return {"files": files}


def drift_message_push(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    st = _active_state()
    if st.get("message_pushed"):
        raise RuntimeError("message_push already called in this drift run")
    st["message_pushed"] = True
    st["draft_message"] = str(arguments.get("message", ""))
    return {"pushed": True, "message": st["draft_message"]}


def drift_finish_drift(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    st = _active_state()
    if st.get("finished"):
        raise RuntimeError("finish_drift already called")
    st["finished"] = True
    decision = str(arguments.get("decision", "complete"))
    msg_result = str(arguments.get("message_result", ""))
    st["decision"] = decision
    st["message_result"] = msg_result
    return {"decision": decision, "message_result": msg_result}
