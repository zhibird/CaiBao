from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from app.core.config import get_settings
from app.services.mcp_client import MCPStdioClient
from app.services.mcp_manager import MCPManager
from app.services.tool_registry import ToolDefinition


# ------------------------------------------------------------------
# Fake MCP server (Python script that speaks JSON-RPC over stdio)
# ------------------------------------------------------------------

_FAKE_MCP_SERVER_SCRIPT = r"""
import json
import sys
import time

def main():
    # 1. Wait for initialize request
    line = sys.stdin.readline()
    req = json.loads(line)
    if req.get("method") == "initialize":
        # Respond with capabilities
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {
            "protocolVersion": req["params"]["protocolVersion"],
            "serverInfo": {"name": "fake-server", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

    # 2. Read notifications/initialized (notification, no id)
    line = sys.stdin.readline()
    # It's a notification — ignore and proceed

    # 3. Respond to tools/list
    line = sys.stdin.readline()
    req = json.loads(line)
    if req.get("method") == "tools/list":
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo back the message.",
                    "inputSchema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {
                            "message": {"type": "string", "minLength": 1},
                        },
                    },
                    "annotations": {"destructiveHint": False},
                },
                {
                    "name": "destroy",
                    "description": "A dangerous operation.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"destructiveHint": True},
                },
            ]
        }}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

    # 4. Handle tools/call requests
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(req, dict):
            continue
        method = req.get("method", "")
        if method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name", "")
            if tool_name == "echo":
                msg = params.get("arguments", {}).get("message", "")
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {
                    "content": [{"type": "text", "text": f"Echo: {msg}"}],
                    "isError": False,
                }}
            else:
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {
                    "content": [{"type": "text", "text": "Done."}],
                    "isError": False,
                }}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
"""


@pytest.fixture
def fake_mcp_server_script(tmp_path):
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_MCP_SERVER_SCRIPT.strip(), encoding="utf-8")
    return str(script)


@pytest.fixture
def mcp_client(fake_mcp_server_script):
    client = MCPStdioClient(
        command=sys.executable,
        args=[fake_mcp_server_script],
        cwd=".",
        env={},
        timeout_seconds=10,
    )
    client.connect()
    yield client
    try:
        client.disconnect()
    except Exception:
        pass


class TestMCPStdioClient:
    def test_connect_discovers_tools(self, mcp_client):
        tools = mcp_client.tools
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert names == {"echo", "destroy"}

    def test_server_info_populated(self, mcp_client):
        info = mcp_client.server_info
        assert info.get("serverInfo", {}).get("name") == "fake-server"

    def test_call_tool_returns_content(self, mcp_client):
        result = mcp_client.call_tool("echo", {"message": "hello"})
        content = result.get("content", [])
        assert len(content) == 1
        assert "Echo: hello" in content[0]["text"]

    def test_is_alive_returns_true(self, mcp_client):
        assert mcp_client.is_alive is True

    def test_disconnect_kills_process(self, fake_mcp_server_script):
        client = MCPStdioClient(
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
        )
        client.connect()
        assert client.is_alive is True
        client.disconnect()
        # Process should be terminated
        assert client.is_alive is False


class TestMCPToolNamespace:
    def test_mcp_manager_discovers_namespaced_tools(self, fake_mcp_server_script):
        manager = MCPManager()
        from app.schemas.mcp import MCPServerConfig

        config = MCPServerConfig(
            name="fake_test",
            enabled=True,
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
        )
        status = manager.connect_all([config])
        assert status["fake_test"]["status"] == "connected"
        assert status["fake_test"]["tool_count"] == 2

        tools = manager.discover_tools()
        namespaced_names = {t.name for t in tools}
        assert "mcp__fake_test__echo" in namespaced_names
        assert "mcp__fake_test__destroy" in namespaced_names

        manager.shutdown_all()

    def test_allowed_tools_filters_mcp_tools(self, fake_mcp_server_script):
        manager = MCPManager()
        from app.schemas.mcp import MCPServerConfig

        config = MCPServerConfig(
            name="filtered_test",
            enabled=True,
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
            allowed_tools=["echo"],
        )
        manager.connect_all([config])
        tools = manager.discover_tools()
        names = {t.name for t in tools}
        assert "mcp__filtered_test__echo" in names
        assert "mcp__filtered_test__destroy" not in names
        manager.shutdown_all()

    def test_dangerous_inferred_from_annotations(self, fake_mcp_server_script):
        manager = MCPManager()
        from app.schemas.mcp import MCPServerConfig

        config = MCPServerConfig(
            name="danger_test",
            enabled=True,
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
        )
        manager.connect_all([config])
        tools = manager.discover_tools()
        echo = next(t for t in tools if t.name == "mcp__danger_test__echo")
        destroy = next(t for t in tools if t.name == "mcp__danger_test__destroy")
        assert echo.dangerous is False
        assert destroy.dangerous is True
        manager.shutdown_all()

    def test_call_tool_via_manager(self, fake_mcp_server_script):
        manager = MCPManager()
        from app.schemas.mcp import MCPServerConfig

        config = MCPServerConfig(
            name="call_test",
            enabled=True,
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
        )
        manager.connect_all([config])
        result = manager.call_tool("mcp__call_test__echo", {"message": "world"})
        assert result["server"] == "call_test"
        assert result["tool"] == "echo"
        assert result["is_error"] is False
        assert len(result["content"]) == 1
        assert "Echo: world" in result["content"][0]["text"]
        manager.shutdown_all()

    def test_server_status_reports_unavailable_after_shutdown(self, fake_mcp_server_script):
        manager = MCPManager()
        from app.schemas.mcp import MCPServerConfig

        config = MCPServerConfig(
            name="status_test",
            enabled=True,
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
        )
        manager.connect_all([config])
        manager.shutdown_all()
        # After shutdown, server should be unavailable
        status = manager.status()
        s = next((x for x in status if x["name"] == "status_test"), None)
        assert s is not None


# ------------------------------------------------------------------
# Server / tool name validation
# ------------------------------------------------------------------

class TestMCPNameValidation:
    def test_server_name_rejects_double_underscore(self):
        from app.schemas.mcp import MCPServerConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MCPServerConfig(name="bad__server", command="echo", args=[])

    def test_server_name_rejects_dots(self):
        from app.schemas.mcp import MCPServerConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MCPServerConfig(name="srv.with.dots", command="echo", args=[])

    def test_server_name_rejects_leading_hyphen(self):
        from app.schemas.mcp import MCPServerConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MCPServerConfig(name="-badstart", command="echo", args=[])

    def test_server_name_rejects_spaces(self):
        from app.schemas.mcp import MCPServerConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MCPServerConfig(name="has space", command="echo", args=[])

    def test_server_name_allows_safe_chars(self):
        from app.schemas.mcp import MCPServerConfig

        cfg = MCPServerConfig(name="my-server_01", command="echo", args=[])
        assert cfg.name == "my-server_01"

    def test_discover_tools_skips_invalid_tool_name(self, fake_mcp_server_script):
        """Tool names with dots or spaces should be skipped."""
        manager = MCPManager()
        from app.schemas.mcp import MCPServerConfig

        config = MCPServerConfig(
            name="name_test",
            enabled=True,
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
        )
        manager.connect_all([config])
        tools = manager.discover_tools()
        # The fake server exposes "echo" and "destroy" — both valid
        for t in tools:
            assert "." not in t.name
            assert " " not in t.name
            assert "__" in t.name  # the namespace separator
        manager.shutdown_all()

    def test_namespaced_name_length_limit(self, fake_mcp_server_script):
        """Long server + tool names must not exceed 64 chars."""
        manager = MCPManager()
        from app.schemas.mcp import MCPServerConfig

        # Server name = 64 chars → namespaced ≥ 64 + len(tool) + 8 > 64
        long_name = "srv" + "x" * 61  # 64 chars total
        config = MCPServerConfig(
            name=long_name,
            enabled=True,
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
        )
        manager.connect_all([config])
        tools = manager.discover_tools()
        for t in tools:
            assert len(t.name) <= 64
        manager.shutdown_all()


# ------------------------------------------------------------------
# Concurrency isolation
# ------------------------------------------------------------------

class TestMCPConcurrency:
    def test_concurrent_call_tool_does_not_mix_responses(self, fake_mcp_server_script):
        """Two calls on the same client must return their own results."""
        import threading

        client = MCPStdioClient(
            command=sys.executable,
            args=[fake_mcp_server_script],
            cwd=".",
            timeout_seconds=10,
        )
        client.connect()
        results = {}
        errors = []

        def call_echo(message, key):
            try:
                r = client.call_tool("echo", {"message": message})
                results[key] = r
            except Exception as exc:
                errors.append((key, str(exc)))

        t1 = threading.Thread(target=call_echo, args=("hello-from-1", "t1"))
        t2 = threading.Thread(target=call_echo, args=("hello-from-2", "t2"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Unexpected errors: {errors}"
        assert results["t1"]["content"][0]["text"] == "Echo: hello-from-1"
        assert results["t2"]["content"][0]["text"] == "Echo: hello-from-2"

        client.disconnect()
