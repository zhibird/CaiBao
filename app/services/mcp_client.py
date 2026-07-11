from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any

from app.core.config import get_settings
from app.core.exceptions import DomainValidationError


class MCPServerError(DomainValidationError):
    """Raised when an MCP server encounters an error."""


class MCPStdioClient:
    """JSON-RPC 2.0 client over stdio for MCP tool servers."""

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        cwd: str = ".",
        env: dict[str, str] | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self.command = command
        self.args = args or []
        self.cwd = cwd
        self.env = env or {}
        self.timeout_seconds = timeout_seconds
        self._proc: subprocess.Popen | None = None
        self._server_info: dict[str, Any] = {}
        self._request_id = 0
        self._tools: list[dict[str, Any]] = []
        self._lock = threading.Lock()  # serialize send/receive across concurrent callers

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Start subprocess and perform initialize → initialized handshake."""
        settings = get_settings()
        full_env = os.environ.copy()
        full_env.update(self.env)

        try:
            self._proc = subprocess.Popen(
                [self.command] + self.args,
                cwd=self.cwd or ".",
                env=full_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # MCP stdio transport mandates UTF-8; without this the pipe
                # codec follows the host locale and non-ASCII tool metadata
                # (e.g. Chinese descriptions) breaks under legacy locales.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except (OSError, FileNotFoundError) as exc:
            raise MCPServerError(f"Failed to start MCP server '{self.command}': {exc}") from exc

        # 1. initialize — clientInfo is required by the MCP spec; official-SDK
        # servers reject the request without it.
        init_result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": settings.app_name or "caibao",
                "version": settings.app_version or "0.0.0",
            },
        }, timeout=max(self.timeout_seconds, settings.mcp_init_timeout_seconds))
        self._server_info = init_result

        # 2. notifications/initialized
        self._send_notification("notifications/initialized", {})

        # 3. tools/list
        tools_result = self._request("tools/list", {}, timeout=max(self.timeout_seconds, settings.mcp_init_timeout_seconds))
        self._tools = tools_result.get("tools", []) if isinstance(tools_result, dict) else []

    def disconnect(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.stdout.close()
                self._proc.stderr.close()
                self._proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    @property
    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    # ------------------------------------------------------------------
    # Tool call
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool via tools/call and return the content."""
        settings = get_settings()
        # Per-server timeout_seconds may extend (never shorten) the global cap,
        # so a slow third-party server can be accommodated per entry.
        result = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout=max(self.timeout_seconds, settings.mcp_call_timeout_seconds))
        return result

    # ------------------------------------------------------------------
    # JSON-RPC 2.0 transport
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the matching response.

        Serialised per client so concurrent call_tool / tools/list callers
        never read each other's response lines from stdout.
        """
        with self._lock:
            self._request_id += 1
            req_id = self._request_id
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            })

            try:
                self._send(request)
            except (BrokenPipeError, OSError) as exc:
                raise MCPServerError(f"MCP server crashed before {method}: {exc}") from exc

            return self._receive_response(expected_id=req_id, timeout=timeout)

    def _send(self, data: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPServerError("MCP server is not connected.")
        self._proc.stdin.write(data + "\n")
        self._proc.stdin.flush()

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        notification = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })
        self._send(notification)

    def _receive_response(self, expected_id: int, timeout: float) -> dict[str, Any]:
        """Read JSON-RPC response from stdout with timeout.

        Uses a watchdog timer that kills the subprocess if stdout.readline()
        blocks past the deadline, so a misbehaving MCP server cannot hang
        the calling thread indefinitely.
        """
        if self._proc is None or self._proc.stdout is None:
            raise MCPServerError("MCP server is not connected.")

        # Watchdog: kill the subprocess on timeout so blocking readline() returns
        watchdog_triggered = threading.Event()

        def _watchdog():
            watchdog_triggered.set()
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except Exception:
                    pass

        timer = threading.Timer(timeout, _watchdog)
        timer.daemon = True
        timer.start()

        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    stderr_output = ""
                    try:
                        if self._proc.stderr:
                            stderr_output = self._proc.stderr.read()
                    except Exception:
                        pass
                    raise MCPServerError(
                        f"MCP server exited with code {self._proc.returncode}. stderr: {stderr_output[:500]}"
                    )

                try:
                    line = self._proc.stdout.readline()
                except Exception as exc:
                    raise MCPServerError(f"Failed to read from MCP server: {exc}") from exc

                if not line:
                    if watchdog_triggered.is_set():
                        raise MCPServerError(f"MCP request timed out after {timeout}s (watchdog killed server)")
                    time.sleep(0.05)
                    continue

                try:
                    msg = json.loads(line.strip())
                except (TypeError, json.JSONDecodeError):
                    continue

                if not isinstance(msg, dict):
                    continue

                msg_id = msg.get("id")
                if msg_id == expected_id:
                    if "error" in msg:
                        err = msg["error"]
                        raise MCPServerError(
                            f"MCP error {err.get('code', '?')}: {err.get('message', str(err))}"
                        )
                    return msg.get("result", {})

                # Ignore messages not addressed to us (notifications from server)

            raise MCPServerError(f"MCP request timed out after {timeout}s")
        finally:
            timer.cancel()
