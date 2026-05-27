from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.core.exceptions import DomainValidationError
from app.services.tool_registry import ToolDefinition

# ------------------------------------------------------------------
# Blacklists
# ------------------------------------------------------------------

_DESTRUCTIVE_COMMANDS = [
    r"\brm\b.+-rf\b.+/",
    r"\brm\b.+-rf\b.+\*",
    r"\bdel\b.+/[sS]",
    r"\bdel\b.+\\[sS]",
    r"\bformat\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bmkfs\b",
    r"\bdd\b.+if=.+of=",
    r"\bdiskpart\b",
    r"\breg\s+delete\b",
    r"\bgit\s+clean\b.+-fdx",
    r"\bchmod\b.+-R\b.+777",
    r"\bchmod\b.+777\b",
    r">\s*/dev/sd",
]

_DESTRUCTIVE_WIN32_COMMANDS = [
    r"\bdel\s+/[fF]\b",
    r"\brmdir\s+/[sS]\b",
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\breg\s+delete\b",
    r"\bstop-process\b.+-force\b",
]

# Network tools blocked unless shell_allow_network=True
_NETWORK_TOOLS = ["curl", "wget", "ssh", "scp", "nc", "ncat", "telnet", "ftp", "sftp"]

# ------------------------------------------------------------------
# Background job registry (in-memory, session lifetime)
# ------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _make_job(command: str, cwd: str, proc: subprocess.Popen) -> str:
    job_id = str(uuid4())[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "command": command,
            "cwd": cwd,
            "proc": proc,
            "started_at": time.time(),
            "status": "running",
            "returncode": None,
            "stdout": b"",
            "stderr": b"",
        }
    return job_id


def _capture_job(job_id: str, proc: subprocess.Popen, timeout_seconds: int = 30) -> None:
    try:
        out, err = proc.communicate(timeout=max(timeout_seconds, 1))
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["stdout"] = out or b""
                _jobs[job_id]["stderr"] = err or b""
                _jobs[job_id]["returncode"] = proc.returncode
                _jobs[job_id]["status"] = "completed" if proc.returncode == 0 else "error"
    except (subprocess.TimeoutExpired, Exception):
        with _jobs_lock:
            if job_id in _jobs:
                try:
                    proc.kill()
                except Exception:
                    pass
                _jobs[job_id]["status"] = "timeout"


# ------------------------------------------------------------------
# Tool definitions
# ------------------------------------------------------------------

_SHELL_EXEC = ToolDefinition(
    name="shell_exec",
    display_name="执行Shell命令",
    description="Execute a shell command in a sandboxed directory. DANGEROUS — requires explicit confirmation.",
    dangerous=True,
    handler_key="generic.shell_exec",
    permission_scope="team",
    source="generic",
    provider="shell_tools",
    input_schema={
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "minLength": 1, "maxLength": 4000},
            "cwd": {"type": "string", "default": "."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 20},
            "background": {"type": "boolean", "default": False},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "returncode": {"type": "integer"},
            "job_id": {"type": "string"},
            "background": {"type": "boolean"},
        },
    },
)

_SHELL_STATUS = ToolDefinition(
    name="shell_status",
    display_name="查询Shell任务状态",
    description="Check the status of a background shell job.",
    dangerous=False,
    handler_key="generic.shell_status",
    permission_scope="team",
    source="generic",
    provider="shell_tools",
    input_schema={
        "type": "object",
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string", "minLength": 1, "maxLength": 64},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "status": {"type": "string"},
            "returncode": {"type": "integer"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
        },
    },
)

_SHELL_KILL = ToolDefinition(
    name="shell_kill",
    display_name="终止Shell任务",
    description="Kill a running background shell job.",
    dangerous=True,
    handler_key="generic.shell_kill",
    permission_scope="team",
    source="generic",
    provider="shell_tools",
    input_schema={
        "type": "object",
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string", "minLength": 1, "maxLength": 64},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "killed": {"type": "boolean"},
        },
    },
)


def create_shell_tools() -> list[ToolDefinition]:
    settings = get_settings()
    if not settings.shell_tool_enabled:
        return []
    return [_SHELL_EXEC, _SHELL_STATUS, _SHELL_KILL]


# ------------------------------------------------------------------
# Safety checks
# ------------------------------------------------------------------

def _check_shell_safety(command: str, *, argv: list[str] | None = None) -> None:
    """Raise DomainValidationError if the command violates safety rules."""
    settings = get_settings()
    lowered = command.strip().lower()

    # Destructive command patterns
    patterns = list(_DESTRUCTIVE_COMMANDS)
    if os.name == "nt":
        patterns = patterns + list(_DESTRUCTIVE_WIN32_COMMANDS)
    for pattern in patterns:
        if re.search(pattern, command, re.IGNORECASE):
            raise DomainValidationError(f"Command blocked by safety policy: matches destructive pattern.")

    # Network commands
    if not settings.shell_allow_network:
        for tool in _NETWORK_TOOLS:
            if re.search(r"\b" + re.escape(tool) + r"\b", lowered):
                raise DomainValidationError(
                    f"Network command '{tool}' is blocked. Set SHELL_ALLOW_NETWORK=true to enable."
                )

    # Block meta-characters for safety: no subshell, no redirect, no pipe
    dangerous_chars = ["`", "$(", "${", ";", "&", ">", ">>", "|"]
    for char in dangerous_chars:
        if char in command:
            raise DomainValidationError(
                f"Shell meta-character '{char}' is not allowed for safety. Use simple single commands."
            )


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------

def _build_shell_cwd(user_cwd: str) -> Path:
    settings = get_settings()
    root = Path(settings.shell_allowed_cwd)
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    target = (root / user_cwd).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise DomainValidationError(f"Shell cwd escapes allowed directory: {user_cwd}")
    return target


def _truncate_output(data: bytes, max_bytes: int) -> str:
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    tail_bytes = int(max_bytes * 0.2)
    return (
        data[: max_bytes - tail_bytes].decode("utf-8", errors="replace")
        + "\n\n[... output truncated ...]\n\n"
        + data[-tail_bytes:].decode("utf-8", errors="replace")
    )


def shell_exec_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    settings = get_settings()
    command = str(arguments["command"]).strip()
    cwd = str(arguments.get("cwd", "."))
    timeout_sec = min(int(arguments.get("timeout_seconds", settings.shell_default_timeout_seconds)), settings.shell_max_timeout_seconds)
    background = bool(arguments.get("background", False))

    _check_shell_safety(command)
    target_cwd = _build_shell_cwd(cwd)

    max_bytes = settings.shell_max_output_bytes

    # Use shell=False with shlex.split for safe argv parsing.
    # On Windows cmd built-in commands (echo, dir, type, ...) are not
    # real executables; if shell=False fails we retry with cmd /d /c
    # while the safety checks above already block meta-characters and
    # destructive patterns.
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise DomainValidationError(f"Invalid shell command syntax: {exc}") from exc

    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(target_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
    except FileNotFoundError:
        if os.name == "nt":
            proc = subprocess.Popen(
                ["cmd", "/d", "/c", command],
                shell=False,
                cwd=str(target_cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )
        else:
            raise DomainValidationError(f"Command not found: {command}")
    except OSError as exc:
        raise DomainValidationError(f"Failed to start command: {exc}") from exc

    if background:
        job_id = _make_job(command, cwd, proc)
        thread = threading.Thread(target=_capture_job, args=(job_id, proc, timeout_sec), daemon=True)
        thread.start()
        return {"job_id": job_id, "background": True, "status": "started", "command": command}

    try:
        out_bytes, err_bytes = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        out_bytes, err_bytes = proc.communicate(timeout=1)
        return {
            "stdout": _truncate_output(out_bytes or b"", max_bytes) if out_bytes else "",
            "stderr": _truncate_output(err_bytes or b"", max_bytes) if err_bytes else "",
            "returncode": -1,
            "timeout": True,
            "background": False,
        }

    return {
        "stdout": _truncate_output(out_bytes, max_bytes),
        "stderr": _truncate_output(err_bytes, max_bytes),
        "returncode": proc.returncode,
        "background": False,
    }


def shell_status_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    job_id = str(arguments["job_id"]).strip()
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise DomainValidationError(f"Job '{job_id}' not found. Jobs are in-memory and lost on restart.")

    proc = job.get("proc")
    if job["status"] == "running" and proc is not None:
        returncode = proc.poll()
        if returncode is not None:
            job["returncode"] = returncode
            job["status"] = "completed" if returncode == 0 else "error"

    max_bytes = get_settings().shell_max_output_bytes
    return {
        "job_id": job_id,
        "status": job["status"],
        "returncode": job["returncode"],
        "command": job["command"],
        "stdout": _truncate_output(job.get("stdout", b""), max_bytes),
        "stderr": _truncate_output(job.get("stderr", b""), max_bytes),
    }


def shell_kill_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    job_id = str(arguments["job_id"]).strip()
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise DomainValidationError(f"Job '{job_id}' not found.")

    proc = job.get("proc")
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    job["status"] = "killed"
    job["returncode"] = -9
    return {"job_id": job_id, "killed": True}
