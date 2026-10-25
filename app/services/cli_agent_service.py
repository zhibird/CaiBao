"""CLI agent backends: delegate an agent run to a local coding-agent CLI.

Supported runners:
- Claude Code: ``claude -p --output-format stream-json``（JSONL 事件流）
- OpenAI Codex: ``codex exec --json``（JSONL 事件流）
- Pi: ``pi -p --mode json``（JSONL 事件流，@mariozechner/pi-coding-agent）

设计要点：
- 与 MCPStdioClient 相同的同步子进程模式（行缓冲 stdout、watchdog 超时、
  stderr 后台收集），但使用进程组终止，避免 CLI 的孙进程残留。
- 每个会话（conversation）对应一个持久工作区目录；CLI 的 session id 记录在
  工作区内的 JSON 文件中，后续同会话的运行自动 resume，实现多轮对话。
- 输出统一解析为 CliAgentEvent，由 AgentService 映射为 AgentStreamEvent /
  AgentStep，从而复用现有 SSE 协议、运行回放与前端渲染。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from app.core.config import Settings
from app.core.exceptions import DomainValidationError

CLI_AGENT_MODES = {"claude_code", "codex", "pi"}

_SESSION_FILE_NAME = ".caibao_cli_sessions.json"
_STDERR_TAIL_LINES = 60
_PATH_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
_MAX_CLAUDE_PLAN_CALLS = 128

# 传给 CLI 子进程的环境变量白名单：仅运行所需的无害变量，绝不含应用密钥。
# 需要额外变量（如 provider API key）时走 CLI_AGENT_EXTRA_ENV_JSON 显式注入。
_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
)


@dataclass(frozen=True)
class CliAgentEvent:
    """Normalized event parsed from a CLI agent's JSONL output."""

    kind: str  # session | text | tool_use | tool_result | plan | warning | result | error
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, object] = field(default_factory=dict)
    tool_output: str = ""
    session_id: str = ""
    ok: bool = True
    plan_source: str = ""
    plan_steps: list[dict[str, str]] = field(default_factory=list)


def _validated_plan_steps(value: object) -> list[dict[str, str]] | None:
    """Validate one complete projection; never truncate a misleading subset."""
    if not isinstance(value, list) or len(value) > 10:
        return None
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    active = 0
    for item in value:
        if not isinstance(item, dict):
            return None
        identifier, content, status = item.get("id"), item.get("content"), item.get("status")
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 64
                or identifier in seen or not isinstance(content, str)
                or not 1 <= len(content) <= 160 or not content.strip()
                or not isinstance(status, str) or status not in _PLAN_STATUSES):
            return None
        active += status == "in_progress"
        if active > 1:
            return None
        seen.add(identifier)
        result.append({"id": identifier, "content": content, "status": status})
    return result


def _claude_todo_steps(todos: object) -> list[dict[str, str]] | None:
    if not isinstance(todos, list) or len(todos) > 10:
        return None
    steps = []
    for index, todo in enumerate(todos, 1):
        if (not isinstance(todo, dict) or not isinstance(todo.get("status"), str)
                or todo["status"] not in {"pending", "in_progress", "completed"}):
            return None
        steps.append({"id": f"s{index}", "content": todo.get("content"), "status": todo["status"]})
    return _validated_plan_steps(steps)


@dataclass
class _ClaudePlanCall:
    name: str
    arguments: dict[str, object]
    received: bool = False
    succeeded: bool = False
    output: object = None


def resolve_cli_workspace(
    settings: Settings,
    *,
    team_id: str,
    conversation_id: str | None,
    run_id: str,
) -> Path:
    """Per-conversation workspace dir the CLI runs in (created on demand)."""
    root = Path(settings.cli_agent_workspace_root).resolve()
    key = conversation_id or f"run-{run_id}"
    workdir = root / _safe_path_component(team_id) / _safe_path_component(key)
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def load_cli_session_id(workdir: Path, runner_name: str) -> str | None:
    from app.services.cli_session_store import load_legacy_cli_session_id
    return load_legacy_cli_session_id(workdir, runner_name)


def save_cli_session_id(workdir: Path, runner_name: str, session_id: str) -> None:
    from app.services.cli_session_store import save_legacy_cli_session_id
    save_legacy_cli_session_id(workdir, runner_name, session_id)


def create_cli_agent_runner(mode: str, settings: Settings) -> "BaseCliAgentRunner":
    if mode == "claude_code":
        return ClaudeCodeRunner(settings)
    if mode == "codex":
        return CodexRunner(settings)
    if mode == "pi":
        return PiRunner(settings)
    raise DomainValidationError(f"未知的 CLI agent 模式：{mode}")


class BaseCliAgentRunner:
    """Spawn a coding-agent CLI and yield normalized events from its JSONL output."""

    name = "cli"
    display_name = "CLI Agent"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- subclass contract -------------------------------------------------
    def build_command(
        self,
        *,
        task: str,
        system_prompt: str,
        resume_session_id: str | None,
        workdir: Path,
        dry_run: bool,
    ) -> tuple[list[str], str]:
        """Return (argv, stdin_payload). Prompt goes via stdin to avoid argv injection."""
        raise NotImplementedError

    def parse_line(self, line: str) -> list[CliAgentEvent]:
        raise NotImplementedError

    def _reset_parser_state(self) -> None:
        """Subclasses may keep correlation state for this dispatch only."""

    # -- shared execution --------------------------------------------------
    def run(
        self,
        *,
        task: str,
        system_prompt: str,
        workdir: Path,
        resume_session_id: str | None,
        dry_run: bool,
    ) -> Iterator[CliAgentEvent]:
        self._reset_parser_state()
        command, stdin_payload = self.build_command(
            task=task, system_prompt=system_prompt,
            resume_session_id=resume_session_id, workdir=workdir, dry_run=dry_run,
        )
        # 不整份继承服务器环境：CLI 是能执行 shell 的自主代理，认证用户可让它
        # `env` 出 AUTH_JWT_SECRET / DATABASE_URL / provider key。只透传运行所需的
        # 无害变量（PATH 定位二进制、HOME 找 ~/.claude ~/.codex OAuth、locale、代理/CA），
        # 密钥一律走显式 CLI_AGENT_EXTRA_ENV_JSON。
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
        env.update(self._extra_env())

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(workdir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise DomainValidationError(
                f"无法启动 {self.display_name} CLI（{command[0]}）：{exc}"
            ) from exc

        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        stderr_thread = threading.Thread(
            target=_drain_lines, args=(proc.stderr, stderr_tail), daemon=True,
        )
        stderr_thread.start()

        try:
            if proc.stdin is not None:
                proc.stdin.write(stdin_payload)
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        timeout_seconds = max(30, int(self.settings.cli_agent_timeout_seconds))
        timed_out = {"flag": False}

        def _on_timeout() -> None:
            timed_out["flag"] = True
            _kill_process_tree(proc)

        watchdog = threading.Timer(timeout_seconds, _on_timeout)
        watchdog.daemon = True
        watchdog.start()

        saw_result = False
        returncode: int | None = None
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                for event in self.parse_line(line):
                    if event.kind == "result":
                        saw_result = True
                    yield event
            # stdout 已 EOF：立即取消看门狗，避免它在 proc.wait 等待窗口内
            # 把一次正常退出误杀成超时。
            watchdog.cancel()
            try:
                returncode = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                returncode = None
        finally:
            watchdog.cancel()
            _kill_process_tree(proc)

        if timed_out["flag"]:
            if saw_result:
                # 结果已完整收到，超时只影响收尾（如子进程清理缓慢）——降级为提示。
                yield CliAgentEvent(
                    kind="warning",
                    text=f"{self.display_name} 在输出最终结果后超时（{timeout_seconds} 秒），残留进程已终止。",
                )
            else:
                yield CliAgentEvent(
                    kind="error", ok=False,
                    text=f"{self.display_name} 执行超时（{timeout_seconds} 秒），进程已终止。",
                )
            return
        if not saw_result:
            # 等 stderr 收集线程读到 EOF，避免竞态导致错误详情为空。
            stderr_thread.join(timeout=1.0)
            stderr_text = "".join(stderr_tail).strip()[-800:]
            if returncode == 0:
                # exit 0 但没有任何可识别的结果事件：不能定性为成功，
                # 否则 run 会被静默标成"完成 + 空答案"。
                detail = (
                    f"{self.display_name} 进程退出（exit=0）但未输出可识别的结果事件，"
                    "CLI 输出格式可能不兼容。"
                )
            else:
                detail = f"{self.display_name} 进程异常退出（exit={returncode}）。"
            if stderr_text:
                detail = f"{detail}\n{stderr_text}"
            yield CliAgentEvent(kind="error", ok=False, text=detail)

    def _extra_env(self) -> dict[str, str]:
        raw = (self.settings.cli_agent_extra_env_json or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # 静默吞掉会让 CLI 在缺 API key 的情况下带着误导性错误运行。
            raise DomainValidationError(
                f"CLI_AGENT_EXTRA_ENV_JSON 不是合法 JSON：{exc}"
            ) from exc
        if not isinstance(data, dict):
            raise DomainValidationError(
                "CLI_AGENT_EXTRA_ENV_JSON 必须是 JSON 对象（如 {\"KEY\": \"value\"}）。"
            )
        return {str(k): str(v) for k, v in data.items()}


class ClaudeCodeRunner(BaseCliAgentRunner):
    name = "claude_code"
    display_name = "Claude Code"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._reset_parser_state()

    def _reset_parser_state(self) -> None:
        self._plan_calls: dict[str, _ClaudePlanCall] = {}
        self._plan_order: deque[str] = deque()
        self._plan_seen: set[str] = set()
        self._plan_tasks: dict[str, dict[str, str]] = {}
        self._plan_next_id = 1
        self._plan_source = ""
        self._plan_signature: tuple[object, ...] | None = None
        self._plan_closed = False

    def build_command(
        self,
        *,
        task: str,
        system_prompt: str,
        resume_session_id: str | None,
        workdir: Path,
        dry_run: bool,
    ) -> tuple[list[str], str]:
        settings = self.settings
        command = [
            settings.cli_agent_claude_path or "claude",
            "-p",
            "--output-format", "stream-json",
            "--verbose",
        ]
        model = (settings.cli_agent_claude_model or "").strip()
        if model:
            command += ["--model", model]
        permission_mode = "plan" if dry_run else (settings.cli_agent_claude_permission_mode or "").strip()
        if permission_mode:
            command += ["--permission-mode", permission_mode]
        allowed_tools = (settings.cli_agent_claude_allowed_tools or "").strip()
        if allowed_tools and not dry_run:
            command += ["--allowedTools", allowed_tools]
        if system_prompt.strip():
            command += ["--append-system-prompt", system_prompt.strip()]
        if resume_session_id:
            command += ["--resume", resume_session_id]
        # Prompt is read from stdin in -p mode.
        return command, task

    def parse_line(self, line: str) -> list[CliAgentEvent]:
        obj = _parse_json_object(line)
        if obj is None:
            return []
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            session_id = str(obj.get("session_id") or "")
            return [CliAgentEvent(kind="session", session_id=session_id)] if session_id else []
        if kind == "assistant":
            return self._parse_assistant(obj)
        if kind == "user":
            return self._parse_tool_results(obj)
        if kind == "result":
            self._plan_closed = True
            self._plan_calls.clear()
            self._plan_order.clear()
            return [CliAgentEvent(
                kind="result",
                ok=not bool(obj.get("is_error")),
                text=str(obj.get("result") or ""),
                session_id=str(obj.get("session_id") or ""),
            )]
        return []

    def _parse_assistant(self, obj: dict[str, object]) -> list[CliAgentEvent]:
        events: list[CliAgentEvent] = []
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                events.append(CliAgentEvent(kind="text", text=str(block["text"])))
            elif block.get("type") == "tool_use":
                tool_input = block.get("input")
                if obj.get("parent_tool_use_id") is None:
                    self._remember_plan_call(block)
                events.append(CliAgentEvent(
                    kind="tool_use",
                    tool_id=str(block.get("id") or ""),
                    tool_name=str(block.get("name") or "tool"),
                    tool_input=tool_input if isinstance(tool_input, dict) else {},
                ))
        return events

    def _parse_tool_results(self, obj: dict[str, object]) -> list[CliAgentEvent]:
        events: list[CliAgentEvent] = []
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        results = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"] if isinstance(content, list) else []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.append(CliAgentEvent(
                    kind="tool_result",
                    tool_id=str(block.get("tool_use_id") or ""),
                    tool_output=_stringify_content(block.get("content")),
                    ok=not bool(block.get("is_error")),
                ))
                if obj.get("parent_tool_use_id") is None and not self._plan_closed:
                    # The structured field is message-scoped. With multiple
                    # result blocks its TaskCreate owner is ambiguous.
                    output = obj.get("tool_use_result") if len(results) == 1 else None
                    events.extend(self._receive_plan_result(block, output))
        return events

    def _remember_plan_call(self, block: dict[str, object]) -> None:
        name, identifier, arguments = block.get("name"), block.get("id"), block.get("input")
        if (self._plan_closed or not isinstance(name, str) or name not in {"TodoWrite", "TaskCreate", "TaskUpdate"}
                or not isinstance(identifier, str) or not identifier
                or identifier in self._plan_seen or len(self._plan_seen) >= _MAX_CLAUDE_PLAN_CALLS):
            return
        self._plan_seen.add(identifier)
        if not isinstance(arguments, dict):
            return
        source = "claude.TodoWrite" if name == "TodoWrite" else "claude.tasks"
        if self._plan_source and self._plan_source != source:
            return
        # Keep only bounded display fields. Description, activeForm, raw tool
        # output and reasoning never become plan presentation data.
        if name == "TodoWrite":
            normalized = _claude_todo_steps(arguments.get("todos"))
            if normalized is None:
                return
            data: dict[str, object] = {"steps": normalized}
        elif name == "TaskCreate":
            subject = arguments.get("subject")
            if _validated_plan_steps([{"id": "s1", "content": subject, "status": "pending"}]) is None:
                return
            data = {"subject": subject}
        else:
            aliases = [arguments[key] for key in ("taskId", "id", "task_id") if key in arguments]
            if (not aliases or not all(isinstance(value, str) and 1 <= len(value) <= 64 for value in aliases)
                    or len(set(aliases)) != 1):
                return
            data = {"task_id": aliases[0]}
            if arguments.get("status") is not None:
                status = arguments["status"]
                if not isinstance(status, str) or status not in {"pending", "in_progress", "completed", "deleted"}:
                    return
                data["status"] = status
            if arguments.get("subject") is not None:
                subject = arguments["subject"]
                if _validated_plan_steps([{"id": "s1", "content": subject, "status": "pending"}]) is None:
                    return
                data["subject"] = subject
            if len(data) == 1:
                return
        self._plan_calls[identifier] = _ClaudePlanCall(name=str(name), arguments=data)
        self._plan_order.append(identifier)

    def _receive_plan_result(self, block: dict[str, object], output: object) -> list[CliAgentEvent]:
        identifier = block.get("tool_use_id")
        if not isinstance(identifier, str):
            return []
        call = self._plan_calls.get(identifier)
        if call is None or call.received:
            return []
        call.received = True
        call.succeeded = block.get("is_error", False) is False
        if isinstance(output, dict):
            if call.name == "TaskCreate":
                task = output.get("task")
                call.output = {"task": {key: task[key] for key in ("id", "subject") if key in task}} if isinstance(task, dict) else None
            elif call.name == "TaskUpdate":
                call.output = {key: output[key] for key in ("success", "taskId", "updatedFields", "error", "statusChange") if key in output}
            elif "newTodos" in output:
                call.output = {"newTodos": output["newTodos"]}
        events: list[CliAgentEvent] = []
        # Match out-of-order results by id, but project in original call order.
        # Missing results block later projections rather than guessing state.
        while self._plan_order and self._plan_calls[self._plan_order[0]].received:
            current = self._plan_calls.pop(self._plan_order.popleft())
            if current.succeeded:
                event = self._apply_plan_result(current)
                if event is not None:
                    events.append(event)
        return events

    def _apply_plan_result(self, call: _ClaudePlanCall) -> CliAgentEvent | None:
        source = "claude.TodoWrite" if call.name == "TodoWrite" else "claude.tasks"
        if self._plan_source and self._plan_source != source:
            return None
        if call.name == "TodoWrite":
            candidate = call.arguments["steps"]
            if isinstance(call.output, dict) and "newTodos" in call.output:
                candidate = _claude_todo_steps(call.output["newTodos"])
                if candidate is None:
                    return None
        else:
            if call.name == "TaskCreate":
                task = call.output.get("task") if isinstance(call.output, dict) else None
                identifier = task.get("id") if isinstance(task, dict) else None
                if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 64
                        or identifier in self._plan_tasks):
                    return None
                subject = task.get("subject", call.arguments["subject"])
                if _validated_plan_steps([{"id": "s1", "content": subject, "status": "pending"}]) is None:
                    return None
                self._plan_tasks[identifier] = {"id": f"s{self._plan_next_id}", "content": subject, "status": "pending"}
                self._plan_next_id += 1
            else:
                identifier = str(call.arguments["task_id"])
                task = self._plan_tasks.get(identifier)
                if task is None:
                    return None
                output = call.output
                if (not isinstance(output, dict) or output.get("success") is not True
                        or output.get("taskId") != identifier or output.get("error")
                        or not isinstance(output.get("updatedFields"), list)):
                    return None
                requested = {key for key in ("subject", "status") if key in call.arguments}
                changed = output["updatedFields"]
                if not all(isinstance(key, str) for key in changed) or not requested.issubset(changed):
                    return None
                change = output.get("statusChange")
                if isinstance(change, dict) and change.get("to") != call.arguments.get("status"):
                    return None
                if call.arguments.get("status") == "deleted":
                    del self._plan_tasks[identifier]
                else:
                    if "status" in requested:
                        task["status"] = str(call.arguments["status"])
                    if "subject" in requested:
                        task["content"] = str(call.arguments["subject"])
            # Retain the observed internal registry even if it currently exceeds
            # display limits. Never publish only its first ten tasks or discard
            # a second active item to make an invalid snapshot appear valid.
            candidate = list(self._plan_tasks.values())
        steps = _validated_plan_steps(candidate)
        if steps is None:
            return None
        signature = (source, tuple((item["id"], item["content"], item["status"]) for item in steps))
        if signature == self._plan_signature:
            return None
        self._plan_source, self._plan_signature = source, signature
        return CliAgentEvent(kind="plan", plan_source=source, plan_steps=steps)


class CodexRunner(BaseCliAgentRunner):
    name = "codex"
    display_name = "Codex"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._reset_parser_state()

    def _reset_parser_state(self) -> None:
        self._plan_closed = False

    def build_command(
        self,
        *,
        task: str,
        system_prompt: str,
        resume_session_id: str | None,
        workdir: Path,
        dry_run: bool,
    ) -> tuple[list[str], str]:
        settings = self.settings
        command = [
            settings.cli_agent_codex_path or "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--color", "never",
            "-C", str(workdir),
        ]
        sandbox = "read-only" if dry_run else (settings.cli_agent_codex_sandbox or "").strip()
        if sandbox:
            command += ["-s", sandbox]
        model = (settings.cli_agent_codex_model or "").strip()
        if model:
            command += ["-m", model]
        if resume_session_id:
            command += ["resume", resume_session_id]
        # "-" 让 codex 从 stdin 读取 prompt。
        command += ["-"]
        prompt = task
        if system_prompt.strip():
            prompt = (
                f"<system_instructions>\n{system_prompt.strip()}\n</system_instructions>\n\n{task}"
            )
        return command, prompt

    def parse_line(self, line: str) -> list[CliAgentEvent]:
        obj = _parse_json_object(line)
        if obj is None:
            return []
        kind = obj.get("type")
        if kind == "thread.started":
            session_id = str(obj.get("thread_id") or "")
            return [CliAgentEvent(kind="session", session_id=session_id)] if session_id else []
        if isinstance(kind, str) and kind in {"item.started", "item.updated", "item.completed"}:
            item = obj.get("item")
            if not isinstance(item, dict):
                return []
            if item.get("type") == "todo_list":
                if self._plan_closed:
                    return []
                items = item.get("items")
                if not isinstance(items, list) or len(items) > 10:
                    return []
                steps = []
                for index, todo in enumerate(items, 1):
                    if not isinstance(todo, dict) or type(todo.get("completed")) is not bool:
                        return []
                    steps.append({"id": f"s{index}", "content": todo.get("text"),
                                  "status": "completed" if todo["completed"] else "pending"})
                normalized = _validated_plan_steps(steps)
                return [CliAgentEvent(kind="plan", plan_source="codex.todo_list", plan_steps=normalized)] if normalized is not None else []
            return self._parse_item(item) if kind == "item.completed" else []
        if kind == "turn.completed":
            self._plan_closed = True
            return [CliAgentEvent(kind="result", ok=True)]
        if kind == "turn.failed":
            self._plan_closed = True
            error = obj.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            return [CliAgentEvent(kind="result", ok=False, text=str(message or "Codex 执行失败"))]
        if kind == "error":
            # 顶层 error 之后通常还有 turn.failed，这里降级为 warning 以免重复失败事件。
            return [CliAgentEvent(kind="warning", text=str(obj.get("message") or ""))]
        return []

    def _parse_item(self, item: dict[str, object]) -> list[CliAgentEvent]:
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            text = str(item.get("text") or "")
            return [CliAgentEvent(kind="text", text=text)] if text else []
        if item_type == "command_execution":
            command = str(item.get("command") or "")
            output = str(item.get("aggregated_output") or "")
            exit_code = item.get("exit_code")
            ok = exit_code in (0, None)
            summary = output if ok else f"exit={exit_code}\n{output}"
            return [
                CliAgentEvent(kind="tool_use", tool_id=item_id, tool_name="shell",
                              tool_input={"command": command}),
                CliAgentEvent(kind="tool_result", tool_id=item_id, tool_output=summary, ok=ok),
            ]
        if item_type == "file_change":
            changes = item.get("changes")
            return [
                CliAgentEvent(kind="tool_use", tool_id=item_id, tool_name="apply_patch",
                              tool_input={"changes": changes if isinstance(changes, (list, dict)) else str(changes or "")}),
                CliAgentEvent(kind="tool_result", tool_id=item_id, tool_output="文件修改已应用"),
            ]
        if item_type == "mcp_tool_call":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "mcp_tool")
            name = f"mcp:{server}.{tool}" if server else f"mcp:{tool}"
            return [
                CliAgentEvent(kind="tool_use", tool_id=item_id, tool_name=name,
                              tool_input={"arguments": item.get("arguments") or {}}),
                CliAgentEvent(kind="tool_result", tool_id=item_id,
                              tool_output=_stringify_content(item.get("result"))),
            ]
        if item_type == "web_search":
            query = str(item.get("query") or "")
            return [
                CliAgentEvent(kind="tool_use", tool_id=item_id, tool_name="web_search",
                              tool_input={"query": query}),
                CliAgentEvent(kind="tool_result", tool_id=item_id, tool_output="搜索完成"),
            ]
        if item_type == "error":
            return [CliAgentEvent(kind="warning", text=str(item.get("message") or ""))]
        # Reasoning is deliberately not projected into task plans or answers.
        return []


class PiRunner(BaseCliAgentRunner):
    name = "pi"
    display_name = "Pi"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        # message_end 里出现 error/aborted 时先记下来，agent_end 时统一定性。
        self._failure_text = ""

    def build_command(
        self,
        *,
        task: str,
        system_prompt: str,
        resume_session_id: str | None,
        workdir: Path,
        dry_run: bool,
    ) -> tuple[list[str], str]:
        settings = self.settings
        session_dir = workdir / ".pi-sessions"
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        command = [
            settings.cli_agent_pi_path or "pi",
            "--print",
            "--mode", "json",
            # 内嵌运行禁用扩展/技能发现：扩展可能弹出无法应答的 UI 对话框。
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--session-dir", str(session_dir),
        ]
        provider = (settings.cli_agent_pi_provider or "").strip()
        if provider:
            command += ["--provider", provider]
        model = (settings.cli_agent_pi_model or "").strip()
        if model:
            command += ["--model", model]
        thinking = (settings.cli_agent_pi_thinking or "").strip()
        if thinking:
            command += ["--thinking", thinking]
        if dry_run:
            command += ["--no-tools"]
        if system_prompt.strip():
            command += ["--append-system-prompt", system_prompt.strip()]
        if resume_session_id:
            command += ["--session", resume_session_id]
        # Prompt is read from stdin in -p mode.
        return command, task

    def parse_line(self, line: str) -> list[CliAgentEvent]:
        obj = _parse_json_object(line)
        if obj is None:
            return []
        kind = obj.get("type")
        if kind == "session":
            session_id = str(obj.get("id") or "")
            return [CliAgentEvent(kind="session", session_id=session_id)] if session_id else []
        if kind == "message_update":
            delta_event = obj.get("assistantMessageEvent")
            if isinstance(delta_event, dict) and delta_event.get("type") == "text_delta":
                delta = str(delta_event.get("delta") or "")
                return [CliAgentEvent(kind="text", text=delta)] if delta else []
            return []
        if kind == "message_end":
            message = obj.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                stop_reason = str(message.get("stopReason") or "")
                if stop_reason in ("error", "aborted"):
                    detail = str(
                        message.get("errorMessage") or message.get("error") or ""
                    ).strip()
                    self._failure_text = detail or f"Pi 执行中断（stopReason={stop_reason}）"
                else:
                    # pi 默认开启自动重试：重试成功后必有一条正常收尾的
                    # assistant 消息，此时清除瞬时错误，避免 agent_end 误判失败。
                    self._failure_text = ""
            return []
        if kind == "tool_execution_start":
            tool_input = obj.get("args")
            return [CliAgentEvent(
                kind="tool_use",
                tool_id=str(obj.get("toolCallId") or ""),
                tool_name=str(obj.get("toolName") or "tool"),
                tool_input=tool_input if isinstance(tool_input, dict) else {},
            )]
        if kind == "tool_execution_end":
            result = obj.get("result")
            content = result.get("content") if isinstance(result, dict) else result
            return [CliAgentEvent(
                kind="tool_result",
                tool_id=str(obj.get("toolCallId") or ""),
                tool_output=_stringify_content(content),
                ok=not bool(obj.get("isError")),
            )]
        if kind == "agent_end":
            if self._failure_text:
                detail = self._failure_text
                self._failure_text = ""
                return [CliAgentEvent(kind="result", ok=False, text=detail)]
            messages = obj.get("messages")
            messages_list = messages if isinstance(messages, list) else []
            # json 模式下 pi 失败时进程仍以 0 退出，ok 标志是唯一失败信号；
            # 循环内异常（handleRunFailure）只发 agent_end 不发 message_end，
            # 因此这里必须再查最后一条 assistant 消息的 stopReason。
            failure = self._final_message_failure(messages_list)
            if failure:
                return [CliAgentEvent(kind="result", ok=False, text=failure)]
            final_text = self._extract_final_text(messages_list)
            return [CliAgentEvent(kind="result", ok=True, text=final_text)]
        if kind == "auto_retry_start":
            detail = str(obj.get("errorMessage") or "")
            attempt = obj.get("attempt")
            max_attempts = obj.get("maxAttempts")
            return [CliAgentEvent(
                kind="warning",
                text=f"Pi 自动重试（{attempt}/{max_attempts}）：{detail}".strip("："),
            )]
        if kind == "extension_error":
            # 仅 --mode rpc 会发出；json 模式下扩展错误只写 stderr。前向兼容保留。
            return [CliAgentEvent(kind="warning", text=str(obj.get("error") or ""))]
        return []

    @staticmethod
    def _final_message_failure(messages: list[object]) -> str:
        """最后一条 assistant 消息以 error/aborted 收尾时返回失败原因，否则空串。"""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            stop_reason = str(message.get("stopReason") or "")
            if stop_reason in ("error", "aborted"):
                detail = str(
                    message.get("errorMessage") or message.get("error") or ""
                ).strip()
                return detail or f"Pi 执行中断（stopReason={stop_reason}）"
            return ""
        return ""

    @staticmethod
    def _extract_final_text(messages: list[object]) -> str:
        """最后一条 assistant 消息的 text 块拼接结果；找不到则回落到流式增量。"""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "".join(parts).strip()
            if text:
                return text
        return ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _safe_path_component(value: str) -> str:
    # 单射清洗：字符替换或截断只要发生信息丢失，就追加原值摘要，
    # 避免不同 team_id（如 "a.b" 与 "a_b"）碰撞到同一工作区目录。
    raw = value or "unknown"
    cleaned = _PATH_SAFE_RE.sub("_", raw)[:48] or "unknown"
    if cleaned != raw:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        cleaned = f"{cleaned}-{digest}"
    return cleaned


def _parse_json_object(line: str) -> dict[str, object] | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _stringify_content(content: object) -> str:
    """Flatten CLI tool-result content (str / block list / arbitrary JSON) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                parts.append(str(text) if text is not None else json.dumps(block, ensure_ascii=False, default=str))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def _drain_lines(stream, sink: deque[str]) -> None:
    try:
        for line in stream:
            sink.append(line)
    except (ValueError, OSError):
        pass


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
        else:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
