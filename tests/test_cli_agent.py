"""CLI agent 后端测试，聚焦 Pi runner（pi -p --mode json）。

假 pi 脚本与 test_mcp_client.py 的假 MCP server 同一套路：
写入 tmp_path 的 Python 脚本通过 shell wrapper 伪装成 pi 可执行文件，
在 stdout 上回放 JSON 事件流，从而无需安装真实 CLI 或调用 LLM。
"""

import json
import stat
import sys

import pytest

from app.core.config import Settings, reload_settings
from app.core.exceptions import DomainValidationError
from app.services.cli_agent_service import (
    CLI_AGENT_MODES,
    ClaudeCodeRunner,
    CodexRunner,
    PiRunner,
    create_cli_agent_runner,
)
from tests.auth_helpers import register_workspace_user

_FAKE_PI_SCRIPT = r"""
import json
import os
import sys


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    task = sys.stdin.read()
    dump_path = os.environ.get("FAKE_PI_ARGV_DUMP")
    if dump_path:
        with open(dump_path, "w", encoding="utf-8") as fh:
            json.dump({
                "argv": sys.argv[1:], "stdin": task,
                "saw_secret": os.environ.get("CAIBAO_FAKE_SECRET"),
                "saw_home": bool(os.environ.get("HOME")),
                "saw_injected": os.environ.get("CAIBAO_INJECTED"),
            }, fh, ensure_ascii=False)

    emit({
        "type": "session", "version": 3, "id": "fake-pi-session-1",
        "timestamp": "2026-07-11T00:00:00.000Z", "cwd": os.getcwd(),
    })

    if "CRASH" in task:
        sys.stderr.write("boom: provider exploded\n")
        sys.exit(3)

    if "SILENT" in task:
        sys.exit(0)

    if task.strip().startswith("HANG"):
        import time
        time.sleep(30)
        sys.exit(0)

    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    emit({
        "type": "message_update", "message": {"role": "assistant"},
        "assistantMessageEvent": {"type": "text_delta", "delta": "正在"},
    })
    emit({
        "type": "message_update", "message": {"role": "assistant"},
        "assistantMessageEvent": {"type": "thinking_delta", "delta": "（内心思考）"},
    })
    emit({
        "type": "message_update", "message": {"role": "assistant"},
        "assistantMessageEvent": {"type": "text_delta", "delta": "处理"},
    })
    emit({
        "type": "tool_execution_start", "toolCallId": "call-1",
        "toolName": "bash", "args": {"command": "ls"},
    })
    emit({
        "type": "tool_execution_end", "toolCallId": "call-1", "toolName": "bash",
        "result": {"content": [{"type": "text", "text": "file.txt"}]},
        "isError": False,
    })
    sys.stdout.write("plain text noise that must be ignored\n")
    sys.stdout.flush()
    final_message = {
        "role": "assistant",
        "content": [
            {"type": "toolCall", "id": "call-1", "name": "bash", "arguments": {}},
            {"type": "text", "text": "最终答案"},
        ],
        "stopReason": "stop",
    }
    emit({"type": "message_end", "message": final_message})
    emit({"type": "turn_end", "message": final_message, "toolResults": []})
    emit({"type": "agent_end", "messages": [final_message]})

    if "RESULT_THEN_HANG" in task:
        import time
        time.sleep(30)


main()
"""


@pytest.fixture
def fake_pi_path(tmp_path):
    """可执行的假 pi：sh wrapper + Python 脚本，路径可直接填进 CLI_AGENT_PI_PATH。"""
    script = tmp_path / "fake_pi_impl.py"
    script.write_text(_FAKE_PI_SCRIPT.strip() + "\n", encoding="utf-8")
    wrapper = tmp_path / "fake_pi"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(wrapper)


def _make_settings(**overrides) -> Settings:
    # 显式给出全部 pi 相关字段：init 参数优先级高于环境变量，
    # 屏蔽开发者 shell 中导出的 CLI_AGENT_PI_* 对单测的影响。
    params = {
        "database_url": "sqlite:///./test_cli_agent.db",
        "cli_agent_pi_path": "pi",
        "cli_agent_pi_provider": "",
        "cli_agent_pi_model": "",
        "cli_agent_pi_thinking": "",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)


class TestPiRunnerRegistration:
    def test_pi_mode_registered(self):
        assert "pi" in CLI_AGENT_MODES

    def test_factory_returns_pi_runner(self):
        runner = create_cli_agent_runner("pi", _make_settings())
        assert isinstance(runner, PiRunner)
        assert runner.name == "pi"

    def test_factory_rejects_unknown_mode(self):
        with pytest.raises(DomainValidationError):
            create_cli_agent_runner("qwen_code", _make_settings())


class TestPiBuildCommand:
    def test_defaults(self, tmp_path):
        runner = PiRunner(_make_settings())
        command, stdin_payload = runner.build_command(
            task="列出文件", system_prompt="", resume_session_id=None,
            workdir=tmp_path, dry_run=False,
        )
        assert command[0] == "pi"
        assert "--print" in command
        mode_index = command.index("--mode")
        assert command[mode_index + 1] == "json"
        for flag in ("--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes"):
            assert flag in command
        session_dir_index = command.index("--session-dir")
        assert command[session_dir_index + 1] == str(tmp_path / ".pi-sessions")
        assert (tmp_path / ".pi-sessions").is_dir()
        for flag in ("--provider", "--model", "--thinking", "--session", "--no-tools"):
            assert flag not in command
        # Prompt 走 stdin，不出现在 argv 中。
        assert stdin_payload == "列出文件"
        assert "列出文件" not in command

    def test_full_flags(self, tmp_path):
        settings = _make_settings(
            cli_agent_pi_path="/opt/bin/pi",
            cli_agent_pi_provider="anthropic",
            cli_agent_pi_model="claude-sonnet-4-5",
            cli_agent_pi_thinking="medium",
        )
        runner = PiRunner(settings)
        command, stdin_payload = runner.build_command(
            task="修个 bug", system_prompt="你是运维助手",
            resume_session_id="abc-123", workdir=tmp_path, dry_run=True,
        )
        assert command[0] == "/opt/bin/pi"
        assert command[command.index("--provider") + 1] == "anthropic"
        assert command[command.index("--model") + 1] == "claude-sonnet-4-5"
        assert command[command.index("--thinking") + 1] == "medium"
        assert command[command.index("--append-system-prompt") + 1] == "你是运维助手"
        assert command[command.index("--session") + 1] == "abc-123"
        assert "--no-tools" in command  # dry_run 降级为纯分析
        assert stdin_payload == "修个 bug"


class TestPiParseLine:
    def _runner(self) -> PiRunner:
        return PiRunner(_make_settings())

    def test_session_header(self):
        events = self._runner().parse_line(
            json.dumps({"type": "session", "version": 3, "id": "sid-1"})
        )
        assert [(e.kind, e.session_id) for e in events] == [("session", "sid-1")]

    def test_text_delta(self):
        runner = self._runner()
        line = json.dumps({
            "type": "message_update", "message": {},
            "assistantMessageEvent": {"type": "text_delta", "delta": "你好"},
        })
        events = runner.parse_line(line)
        assert [(e.kind, e.text) for e in events] == [("text", "你好")]

    def test_thinking_delta_ignored(self):
        line = json.dumps({
            "type": "message_update", "message": {},
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "思考"},
        })
        assert self._runner().parse_line(line) == []

    def test_tool_events(self):
        runner = self._runner()
        start = runner.parse_line(json.dumps({
            "type": "tool_execution_start", "toolCallId": "c1",
            "toolName": "bash", "args": {"command": "ls"},
        }))
        assert start[0].kind == "tool_use"
        assert start[0].tool_id == "c1"
        assert start[0].tool_name == "bash"
        assert start[0].tool_input == {"command": "ls"}

        end = runner.parse_line(json.dumps({
            "type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "输出内容"}]},
            "isError": True,
        }))
        assert end[0].kind == "tool_result"
        assert end[0].tool_output == "输出内容"
        assert end[0].ok is False

    def test_agent_end_extracts_final_text(self):
        runner = self._runner()
        events = runner.parse_line(json.dumps({
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "任务"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "推理"},
                        {"type": "text", "text": "答案A"},
                        {"type": "text", "text": "答案B"},
                    ],
                    "stopReason": "stop",
                },
            ],
        }))
        assert [(e.kind, e.ok, e.text) for e in events] == [("result", True, "答案A答案B")]

    def test_stop_reason_error_marks_failure(self):
        runner = self._runner()
        assert runner.parse_line(json.dumps({
            "type": "message_end",
            "message": {"role": "assistant", "content": [], "stopReason": "error",
                        "errorMessage": "上游 500"},
        })) == []
        events = runner.parse_line(json.dumps({"type": "agent_end", "messages": []}))
        assert [(e.kind, e.ok, e.text) for e in events] == [("result", False, "上游 500")]

    def test_auto_retry_recovery_ends_ok(self):
        """瞬时错误 → 自动重试成功：最终 result 必须 ok=True 且携带真实答案。

        pi 默认开启自动重试，且 agent_end 在重试判定前就已发到 JSON 流，
        因此事件序列为：message_end(error) → agent_end → auto_retry_start
        → 重试运行 → message_end(stop) → agent_end。
        """
        runner = self._runner()
        assert runner.parse_line(json.dumps({
            "type": "message_end",
            "message": {"role": "assistant", "content": [], "stopReason": "error",
                        "errorMessage": "529 overloaded"},
        })) == []
        first = runner.parse_line(json.dumps({"type": "agent_end", "messages": []}))
        assert [(e.kind, e.ok) for e in first] == [("result", False)]
        retry = runner.parse_line(json.dumps({
            "type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
            "delayMs": 1000, "errorMessage": "529 overloaded",
        }))
        assert retry[0].kind == "warning"
        assert runner.parse_line(json.dumps({
            "type": "message_end",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "重试后的答案"}],
                        "stopReason": "stop"},
        })) == []
        final = runner.parse_line(json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant",
                          "content": [{"type": "text", "text": "重试后的答案"}],
                          "stopReason": "stop"}],
        }))
        assert [(e.kind, e.ok, e.text) for e in final] == [("result", True, "重试后的答案")]

    def test_agent_end_failure_without_message_end(self):
        """pi 循环内异常（handleRunFailure）只发 agent_end：必须定性为失败。

        json 模式下 pi 失败时进程仍以 0 退出，此路径若误判 ok=True，
        run 会被静默标成"完成 + 空答案"。
        """
        events = self._runner().parse_line(json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant",
                          "content": [{"type": "text", "text": ""}],
                          "stopReason": "error",
                          "errorMessage": "context conversion failed"}],
        }))
        assert [(e.kind, e.ok, e.text) for e in events] == [
            ("result", False, "context conversion failed"),
        ]

    def test_auto_retry_warning(self):
        events = self._runner().parse_line(json.dumps({
            "type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
            "delayMs": 2000, "errorMessage": "529 overloaded",
        }))
        assert events[0].kind == "warning"
        assert "529 overloaded" in events[0].text

    def test_garbage_and_unknown_lines(self):
        runner = self._runner()
        assert runner.parse_line("not json at all") == []
        assert runner.parse_line(json.dumps({"type": "turn_start"})) == []


class TestPiRunnerIntegration:
    def test_happy_path(self, fake_pi_path, tmp_path, monkeypatch):
        argv_dump = tmp_path / "argv.json"
        # FAKE_PI_ARGV_DUMP 不在 env 白名单里，必须走 CLI_AGENT_EXTRA_ENV_JSON
        # 显式注入——顺带验证白名单外的进程环境变量不会自动透传给子进程。
        monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(argv_dump))
        runner = PiRunner(_make_settings(
            cli_agent_pi_path=fake_pi_path,
            cli_agent_extra_env_json=json.dumps({"FAKE_PI_ARGV_DUMP": str(argv_dump)}),
        ))
        workdir = tmp_path / "ws"
        workdir.mkdir()

        events = list(runner.run(
            task="整理目录", system_prompt="", workdir=workdir,
            resume_session_id=None, dry_run=False,
        ))

        kinds = [e.kind for e in events]
        assert kinds == ["session", "text", "text", "tool_use", "tool_result", "result"]
        assert events[0].session_id == "fake-pi-session-1"
        assert [e.text for e in events if e.kind == "text"] == ["正在", "处理"]
        assert events[3].tool_name == "bash"
        assert events[4].tool_output == "file.txt"
        assert events[5].ok is True
        assert events[5].text == "最终答案"

        dumped = json.loads(argv_dump.read_text(encoding="utf-8"))
        assert dumped["stdin"] == "整理目录"
        argv = dumped["argv"]
        assert "--print" in argv
        assert argv[argv.index("--mode") + 1] == "json"

    def test_env_allowlist_blocks_secrets_but_keeps_essentials(
        self, fake_pi_path, tmp_path, monkeypatch
    ):
        """子进程只拿到白名单变量 + EXTRA_ENV_JSON 注入，进程环境里的密钥不透传。"""
        argv_dump = tmp_path / "argv.json"
        # 模拟服务器进程环境里存在的敏感变量（AUTH_JWT_SECRET 之类的替身）。
        monkeypatch.setenv("CAIBAO_FAKE_SECRET", "super-secret-jwt")
        runner = PiRunner(_make_settings(
            cli_agent_pi_path=fake_pi_path,
            cli_agent_extra_env_json=json.dumps({
                "FAKE_PI_ARGV_DUMP": str(argv_dump),
                "CAIBAO_INJECTED": "ok",
            }),
        ))
        workdir = tmp_path / "ws"
        workdir.mkdir()

        list(runner.run(
            task="检查环境", system_prompt="", workdir=workdir,
            resume_session_id=None, dry_run=False,
        ))

        dumped = json.loads(argv_dump.read_text(encoding="utf-8"))
        assert dumped["saw_secret"] is None      # 白名单外的进程环境变量被拦截
        assert dumped["saw_home"] is True         # 白名单变量（HOME）保留
        assert dumped["saw_injected"] == "ok"     # EXTRA_ENV_JSON 显式注入生效

    def test_crash_surfaces_stderr(self, fake_pi_path, tmp_path):
        runner = PiRunner(_make_settings(cli_agent_pi_path=fake_pi_path))
        workdir = tmp_path / "ws"
        workdir.mkdir()

        events = list(runner.run(
            task="CRASH now", system_prompt="", workdir=workdir,
            resume_session_id=None, dry_run=False,
        ))

        assert events[0].kind == "session"
        assert events[-1].kind == "error"
        assert events[-1].ok is False
        assert "boom: provider exploded" in events[-1].text

    def test_silent_zero_exit_is_error(self, fake_pi_path, tmp_path):
        """CLI 以 0 退出但没有可识别的结果事件：必须定性为失败而非"完成 + 空答案"。"""
        runner = PiRunner(_make_settings(cli_agent_pi_path=fake_pi_path))
        workdir = tmp_path / "ws"
        workdir.mkdir()

        events = list(runner.run(
            task="SILENT run", system_prompt="", workdir=workdir,
            resume_session_id=None, dry_run=False,
        ))

        assert events[0].kind == "session"
        assert events[-1].kind == "error"
        assert events[-1].ok is False
        assert "未输出可识别的结果事件" in events[-1].text

    def test_timeout_after_result_downgrades_to_warning(self, fake_pi_path, tmp_path, monkeypatch):
        """结果已完整收到后才超时（如子进程收尾缓慢）：不得把成功翻转为失败。"""
        import threading as _threading

        import app.services.cli_agent_service as cli_mod

        real_timer = _threading.Timer

        def _fast_timer(_interval, fn):
            return real_timer(2.0, fn)

        monkeypatch.setattr(cli_mod.threading, "Timer", _fast_timer)

        runner = PiRunner(_make_settings(cli_agent_pi_path=fake_pi_path))
        workdir = tmp_path / "ws"
        workdir.mkdir()

        events = list(runner.run(
            task="整理 RESULT_THEN_HANG", system_prompt="", workdir=workdir,
            resume_session_id=None, dry_run=False,
        ))

        result = next(e for e in events if e.kind == "result")
        assert result.ok is True
        assert result.text == "最终答案"
        assert events[-1].kind == "warning"
        assert "超时" in events[-1].text

    def test_watchdog_kills_hung_pi(self, fake_pi_path, tmp_path, monkeypatch):
        """挂死的 pi 由 watchdog 杀进程组并产出超时 error 事件。

        cli_agent_timeout_seconds 有 max(30, ...) 下限，测试中把 Timer
        替换为固定短延迟触发，避免真等 30 秒。
        """
        import threading as _threading

        import app.services.cli_agent_service as cli_mod

        real_timer = _threading.Timer

        def _fast_timer(_interval, fn):
            # 2 秒：既远小于 30 秒下限，又给解释器启动和 session 头留足余量。
            return real_timer(2.0, fn)

        monkeypatch.setattr(cli_mod.threading, "Timer", _fast_timer)

        runner = PiRunner(_make_settings(cli_agent_pi_path=fake_pi_path))
        workdir = tmp_path / "ws"
        workdir.mkdir()

        events = list(runner.run(
            task="HANG forever", system_prompt="", workdir=workdir,
            resume_session_id=None, dry_run=False,
        ))

        assert events[0].kind == "session"
        assert events[-1].kind == "error"
        assert events[-1].ok is False
        assert "超时" in events[-1].text


class TestPiRunModeViaApi:
    def test_agent_run_stream_with_pi_backend(self, client, fake_pi_path, tmp_path, monkeypatch):
        monkeypatch.setenv("CLI_AGENT_ENABLED", "true")
        monkeypatch.setenv("CLI_AGENT_PI_PATH", fake_pi_path)
        monkeypatch.setenv("CLI_AGENT_WORKSPACE_ROOT", str(tmp_path / "cli_ws"))
        reload_settings()

        register_workspace_user(client, prefix="piuser", display_name="Pi User")

        create_response = client.post(
            "/api/v1/agent/runs",
            json={"task": "帮我整理目录", "run_mode": "pi"},
        )
        assert create_response.status_code == 200, create_response.text
        run_id = create_response.json()["run_id"]

        stream_response = client.get(f"/api/v1/agent/runs/{run_id}/stream")
        assert stream_response.status_code == 200
        body = stream_response.text
        # 断言事件相对顺序，而不只是出现过。
        positions = [
            body.index("event: run.started"),
            body.index("event: llm.delta"),
            body.index("event: tool.started"),
            body.index("event: tool.result"),
            body.index("event: run.completed"),
        ]
        assert positions == sorted(positions)
        assert "最终答案" in body

        run_response = client.get(f"/api/v1/agent/runs/{run_id}")
        assert run_response.status_code == 200
        run_body = run_response.json()
        assert run_body["status"] == "completed"
        assert run_body["answer"] == "最终答案"

    def test_session_resume_across_runs(self, client, fake_pi_path, tmp_path, monkeypatch):
        """带 conversation_id 的两次运行：第一次落盘 session id，第二次以 --session 续接。"""
        workspace_root = tmp_path / "cli_ws"
        argv_dump = tmp_path / "argv.json"
        monkeypatch.setenv("CLI_AGENT_ENABLED", "true")
        monkeypatch.setenv("CLI_AGENT_PI_PATH", fake_pi_path)
        monkeypatch.setenv("CLI_AGENT_WORKSPACE_ROOT", str(workspace_root))
        # FAKE_PI_ARGV_DUMP 不在 env 白名单，走 EXTRA_ENV_JSON 注入子进程。
        monkeypatch.setenv("CLI_AGENT_EXTRA_ENV_JSON", json.dumps({"FAKE_PI_ARGV_DUMP": str(argv_dump)}))
        reload_settings()

        team_id, _user_id = register_workspace_user(
            client, prefix="piuser3", display_name="Pi User 3"
        )
        conversation = client.post("/api/v1/conversations", json={"title": "pi 多轮"})
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["conversation_id"]

        def _run_once() -> None:
            create = client.post(
                "/api/v1/agent/runs",
                json={"task": "继续整理", "run_mode": "pi",
                      "conversation_id": conversation_id},
            )
            assert create.status_code == 200, create.text
            stream = client.get(f"/api/v1/agent/runs/{create.json()['run_id']}/stream")
            assert "event: run.completed" in stream.text

        _run_once()
        session_file = (
            workspace_root / team_id / conversation_id / ".caibao_cli_sessions.json"
        )
        assert session_file.is_file()
        assert json.loads(session_file.read_text(encoding="utf-8"))["pi"] == "fake-pi-session-1"

        _run_once()
        argv = json.loads(argv_dump.read_text(encoding="utf-8"))["argv"]
        assert argv[argv.index("--session") + 1] == "fake-pi-session-1"

    def test_pi_mode_rejected_when_disabled(self, client, tmp_path, monkeypatch):
        # 显式置 false 而非 delenv：防止开发者 .env 里的 CLI_AGENT_ENABLED=true
        # 在 reload 后生效，导致此测试真的拉起 pi。
        monkeypatch.setenv("CLI_AGENT_ENABLED", "false")
        monkeypatch.setenv("CLI_AGENT_WORKSPACE_ROOT", str(tmp_path / "cli_ws"))
        reload_settings()

        register_workspace_user(client, prefix="piuser2", display_name="Pi User 2")

        create_response = client.post(
            "/api/v1/agent/runs",
            json={"task": "帮我整理目录", "run_mode": "pi"},
        )
        assert create_response.status_code == 200, create_response.text
        run_id = create_response.json()["run_id"]

        stream_response = client.get(f"/api/v1/agent/runs/{run_id}/stream")
        assert stream_response.status_code == 200
        assert "event: run.failed" in stream_response.text
        assert "CLI agent" in stream_response.text

    def test_unknown_run_mode_rejected_with_400(self, client, monkeypatch):
        """拼错的 run_mode（如 claude-code）应 400，而非静默降级到内置循环。"""
        monkeypatch.setenv("CLI_AGENT_ENABLED", "false")
        reload_settings()

        register_workspace_user(client, prefix="piuser4", display_name="Pi User 4")

        response = client.post(
            "/api/v1/agent/runs",
            json={"task": "帮我整理目录", "run_mode": "claude-code"},
        )
        assert response.status_code == 400
        assert "Unknown run_mode" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Claude Code / Codex runner：命令构造与事件解析
# （子进程执行骨架为三 runner 共享，已由上方假 pi 集成测试覆盖，
#   这里只测各 runner 的两个纯函数。）
# ---------------------------------------------------------------------------

def _make_claude_settings(**overrides) -> Settings:
    params = {
        "database_url": "sqlite:///./test_cli_agent.db",
        "cli_agent_claude_path": "claude",
        "cli_agent_claude_model": "",
        "cli_agent_claude_permission_mode": "acceptEdits",
        "cli_agent_claude_allowed_tools": "",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)


def _make_codex_settings(**overrides) -> Settings:
    params = {
        "database_url": "sqlite:///./test_cli_agent.db",
        "cli_agent_codex_path": "codex",
        "cli_agent_codex_model": "",
        "cli_agent_codex_sandbox": "workspace-write",
    }
    params.update(overrides)
    return Settings(_env_file=None, **params)


class TestClaudeCodeBuildCommand:
    def test_defaults(self, tmp_path):
        runner = ClaudeCodeRunner(_make_claude_settings())
        command, stdin_payload = runner.build_command(
            task="列出文件", system_prompt="", resume_session_id=None,
            workdir=tmp_path, dry_run=False,
        )
        assert command[0] == "claude"
        assert "-p" in command
        assert command[command.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in command
        assert command[command.index("--permission-mode") + 1] == "acceptEdits"
        for flag in ("--model", "--allowedTools", "--append-system-prompt", "--resume"):
            assert flag not in command
        # Prompt 走 stdin，不出现在 argv 中。
        assert stdin_payload == "列出文件"
        assert "列出文件" not in command

    def test_full_flags(self, tmp_path):
        settings = _make_claude_settings(
            cli_agent_claude_path="/opt/bin/claude",
            cli_agent_claude_model="claude-sonnet-5",
            cli_agent_claude_allowed_tools="Read,Grep",
        )
        command, stdin_payload = ClaudeCodeRunner(settings).build_command(
            task="修个 bug", system_prompt="你是运维助手",
            resume_session_id="sid-1", workdir=tmp_path, dry_run=False,
        )
        assert command[0] == "/opt/bin/claude"
        assert command[command.index("--model") + 1] == "claude-sonnet-5"
        assert command[command.index("--allowedTools") + 1] == "Read,Grep"
        assert command[command.index("--append-system-prompt") + 1] == "你是运维助手"
        assert command[command.index("--resume") + 1] == "sid-1"
        assert stdin_payload == "修个 bug"

    def test_dry_run_forces_plan_mode_and_drops_allowed_tools(self, tmp_path):
        settings = _make_claude_settings(cli_agent_claude_allowed_tools="Bash,Edit")
        command, _ = ClaudeCodeRunner(settings).build_command(
            task="分析", system_prompt="", resume_session_id=None,
            workdir=tmp_path, dry_run=True,
        )
        assert command[command.index("--permission-mode") + 1] == "plan"
        assert "--allowedTools" not in command


class TestClaudeCodeParseLine:
    def _runner(self) -> ClaudeCodeRunner:
        return ClaudeCodeRunner(_make_claude_settings())

    def test_init_session(self):
        events = self._runner().parse_line(json.dumps({
            "type": "system", "subtype": "init", "session_id": "sid-9",
        }))
        assert [(e.kind, e.session_id) for e in events] == [("session", "sid-9")]

    def test_assistant_text_and_tool_use(self):
        events = self._runner().parse_line(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "先看下目录"},
                {"type": "tool_use", "id": "tu-1", "name": "Bash",
                 "input": {"command": "ls"}},
            ]},
        }))
        assert [(e.kind) for e in events] == ["text", "tool_use"]
        assert events[0].text == "先看下目录"
        assert events[1].tool_id == "tu-1"
        assert events[1].tool_name == "Bash"
        assert events[1].tool_input == {"command": "ls"}

    def test_tool_result_flattens_blocks_and_error_flag(self):
        events = self._runner().parse_line(json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu-1", "is_error": True,
                 "content": [{"type": "text", "text": "命令失败"}]},
            ]},
        }))
        assert [(e.kind, e.tool_id, e.tool_output, e.ok) for e in events] == [
            ("tool_result", "tu-1", "命令失败", False),
        ]

    def test_result_success_and_error(self):
        runner = self._runner()
        ok = runner.parse_line(json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "搞定", "session_id": "sid-9",
        }))
        assert [(e.kind, e.ok, e.text, e.session_id) for e in ok] == [
            ("result", True, "搞定", "sid-9"),
        ]
        bad = runner.parse_line(json.dumps({
            "type": "result", "subtype": "success", "is_error": True,
            "result": "Not logged in · Please run /login", "session_id": "sid-9",
        }))
        assert bad[0].ok is False
        assert "Not logged in" in bad[0].text

    def test_garbage_and_unknown_lines(self):
        runner = self._runner()
        assert runner.parse_line("plain noise") == []
        assert runner.parse_line(json.dumps({"type": "stream_event"})) == []


class TestCodexBuildCommand:
    def test_defaults(self, tmp_path):
        runner = CodexRunner(_make_codex_settings())
        command, stdin_payload = runner.build_command(
            task="列出文件", system_prompt="", resume_session_id=None,
            workdir=tmp_path, dry_run=False,
        )
        assert command[:2] == ["codex", "exec"]
        assert "--json" in command
        assert "--skip-git-repo-check" in command
        assert command[command.index("-C") + 1] == str(tmp_path)
        assert command[command.index("-s") + 1] == "workspace-write"
        assert "-m" not in command
        # "-" 结尾表示 prompt 从 stdin 读取。
        assert command[-1] == "-"
        assert stdin_payload == "列出文件"
        assert "列出文件" not in command

    def test_dry_run_uses_read_only_sandbox(self, tmp_path):
        command, _ = CodexRunner(_make_codex_settings()).build_command(
            task="分析", system_prompt="", resume_session_id=None,
            workdir=tmp_path, dry_run=True,
        )
        assert command[command.index("-s") + 1] == "read-only"

    def test_resume_subcommand_order_and_system_prompt(self, tmp_path):
        settings = _make_codex_settings(cli_agent_codex_model="gpt-5-codex")
        command, stdin_payload = CodexRunner(settings).build_command(
            task="继续", system_prompt="你是运维助手",
            resume_session_id="tid-7", workdir=tmp_path, dry_run=False,
        )
        assert command[command.index("-m") + 1] == "gpt-5-codex"
        # resume 是 exec 的子命令：`codex exec [OPTIONS] resume <id> -`
        assert command[-3:] == ["resume", "tid-7", "-"]
        # codex 无 system prompt 参数，注入到 stdin prompt 头部。
        assert stdin_payload.startswith("<system_instructions>\n你是运维助手")
        assert stdin_payload.endswith("继续")


class TestCodexParseLine:
    def _runner(self) -> CodexRunner:
        return CodexRunner(_make_codex_settings())

    def test_thread_started(self):
        events = self._runner().parse_line(json.dumps({
            "type": "thread.started", "thread_id": "tid-7",
        }))
        assert [(e.kind, e.session_id) for e in events] == [("session", "tid-7")]

    def test_agent_message(self):
        events = self._runner().parse_line(json.dumps({
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "完成"},
        }))
        assert [(e.kind, e.text) for e in events] == [("text", "完成")]

    def test_command_execution_success_and_failure(self):
        runner = self._runner()
        ok = runner.parse_line(json.dumps({
            "type": "item.completed",
            "item": {"id": "item_2", "type": "command_execution",
                     "command": "ls", "aggregated_output": "hello.txt", "exit_code": 0},
        }))
        assert [e.kind for e in ok] == ["tool_use", "tool_result"]
        assert ok[0].tool_name == "shell"
        assert ok[0].tool_input == {"command": "ls"}
        assert ok[1].tool_output == "hello.txt"
        assert ok[1].ok is True

        bad = runner.parse_line(json.dumps({
            "type": "item.completed",
            "item": {"id": "item_3", "type": "command_execution",
                     "command": "false", "aggregated_output": "", "exit_code": 1},
        }))
        assert bad[1].ok is False
        assert "exit=1" in bad[1].tool_output

    def test_file_change(self):
        events = self._runner().parse_line(json.dumps({
            "type": "item.completed",
            "item": {"id": "item_4", "type": "file_change",
                     "changes": [{"path": "a.txt", "kind": "add"}]},
        }))
        assert events[0].tool_name == "apply_patch"
        assert events[1].kind == "tool_result"

    def test_turn_completed_and_failed(self):
        runner = self._runner()
        done = runner.parse_line(json.dumps({"type": "turn.completed", "usage": {}}))
        assert [(e.kind, e.ok) for e in done] == [("result", True)]
        failed = runner.parse_line(json.dumps({
            "type": "turn.failed", "error": {"message": "model unsupported"},
        }))
        assert [(e.kind, e.ok) for e in failed] == [("result", False)]
        assert "model unsupported" in failed[0].text

    def test_top_level_and_item_error_degrade_to_warning(self):
        runner = self._runner()
        top = runner.parse_line(json.dumps({"type": "error", "message": "400 bad request"}))
        assert [(e.kind,) for e in top] == [("warning",)]
        item = runner.parse_line(json.dumps({
            "type": "item.completed",
            "item": {"id": "item_5", "type": "error", "message": "metadata missing"},
        }))
        assert [(e.kind,) for e in item] == [("warning",)]

    def test_reasoning_and_garbage_ignored(self):
        runner = self._runner()
        assert runner.parse_line(json.dumps({
            "type": "item.completed",
            "item": {"id": "item_6", "type": "reasoning", "text": "思考中"},
        })) == []
        assert runner.parse_line("Reading additional input from stdin...") == []
