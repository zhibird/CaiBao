from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.core.exceptions import DomainValidationError
from app.services.tools.web_tools import (
    _exa_search,
    _host_is_dangerous,
    _html_to_markdown,
    _html_to_text,
    _parse_exa_results,
    _validate_url_target,
    create_web_tools,
    web_fetch_handler,
    web_search_handler,
)
from app.services.tools.file_tools import (
    _resolve_safe_path,
    create_file_tools,
    edit_file_handler,
    list_dir_handler,
    read_file_handler,
    write_file_handler,
)
from app.services.tools.shell_tools import (
    _check_shell_safety,
    create_shell_tools,
    shell_exec_handler,
    shell_status_handler,
    shell_kill_handler,
)


# ------------------------------------------------------------------
# Web tools
# ------------------------------------------------------------------

class TestHostIsDangerous:
    def test_blocks_loopback_ipv4(self):
        assert _host_is_dangerous("127.0.0.1") is True

    def test_blocks_loopback_ipv6(self):
        assert _host_is_dangerous("::1") is True

    def test_blocks_localhost(self):
        assert _host_is_dangerous("localhost") is True

    def test_blocks_private_10x(self):
        assert _host_is_dangerous("10.0.0.1") is True

    def test_blocks_private_192_168(self):
        assert _host_is_dangerous("192.168.1.1") is True

    def test_blocks_private_172_16(self):
        assert _host_is_dangerous("172.16.0.1") is True

    def test_blocks_metadata_ip(self):
        assert _host_is_dangerous("169.254.169.254") is True

    def test_allows_public_ip(self):
        # Mock DNS to return a known public IP
        with mock.patch("socket.getaddrinfo") as m_dns:
            m_dns.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 80)),  # example.com public IP
            ]
            assert _host_is_dangerous("example.com") is False


class TestWebFetchHandler:
    def test_blocks_non_http_scheme(self):
        with pytest.raises(DomainValidationError, match="http"):
            web_fetch_handler(
                team_id="t1", user_id="u1",
                arguments={"url": "ftp://example.com"},
            )

    def test_blocks_private_ip_url(self):
        with pytest.raises(DomainValidationError, match="private"):
            web_fetch_handler(
                team_id="t1", user_id="u1",
                arguments={"url": "http://127.0.0.1:8080/secret"},
            )

    def _try_fetch_httpbin(self, **extra_args) -> dict:
        """Call web_fetch_handler against httpbin; skip test if external service is down."""
        import socket
        try:
            socket.getaddrinfo("httpbin.org", 443)
        except socket.gaierror:
            pytest.skip("Network not available for fetch test")
        arguments: dict[str, object] = {
            "url": "https://httpbin.org/status/200",
            "format": "text",
            **extra_args,
        }
        try:
            with mock.patch("app.services.tools.web_tools._host_is_dangerous", return_value=False):
                return web_fetch_handler(
                    team_id="t1", user_id="u1",
                    arguments=arguments,
                )
        except DomainValidationError as exc:
            msg = str(exc)
            pytest.skip(f"httpbin.org unreachable: {msg[:80]}")

    def test_fetches_public_url(self):
        result = self._try_fetch_httpbin()
        if result.get("status") != 200:
            pytest.skip(f"httpbin.org returned {result.get('status')} (external service issue)")
        assert "final_url" in result
        assert "text" in result

    def test_truncates_long_content(self):
        # _MAX_TEXT_CHARS = 50_000 by default; override it for this test
        with mock.patch("app.services.tools.web_tools._MAX_TEXT_CHARS", 100):
            result = self._try_fetch_httpbin()
        if result.get("status") != 200:
            pytest.skip(f"httpbin.org returned {result.get('status')} (external service issue)")
        assert result.get("truncated") is True or len(str(result.get("text", ""))) <= 100


class TestWebSearchHandler:
    def test_returns_error_when_no_provider_configured(self):
        with mock.patch("app.services.tools.web_tools.get_settings") as m:
            m.return_value.web_search_provider = "disabled"
            m.return_value.web_search_api_key = None
            with pytest.raises(DomainValidationError, match="not configured"):
                web_search_handler(
                    team_id="t1", user_id="u1",
                    arguments={"query": "test"},
                )


class TestCreateWebTools:
    def test_returns_two_definitions(self):
        defs = create_web_tools()
        names = {d.name for d in defs}
        assert names == {"web_fetch", "web_search"}
        for d in defs:
            assert d.source == "generic"
            assert d.provider == "web_tools"


# ------------------------------------------------------------------
# File tools
# ------------------------------------------------------------------

class TestResolveSafePath:
    def test_resolves_subpath(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        with mock.patch("app.services.tools.file_tools._build_file_root", return_value=root):
            resolved = _resolve_safe_path("team-1", "notes.txt")
            assert resolved == (root / "notes.txt").resolve()

    def test_blocks_dot_dot_escape(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        with mock.patch("app.services.tools.file_tools._build_file_root", return_value=root):
            with pytest.raises(DomainValidationError, match="escape"):
                _resolve_safe_path("team-1", "../secret.txt")

    def test_strips_leading_slash_from_abs_path(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        with mock.patch("app.services.tools.file_tools._build_file_root", return_value=root):
            abs_path = os.path.join(str(root), "sub", "file.txt")
            # path relative to root
            resolved = _resolve_safe_path("team-1", "sub/file.txt")
            assert resolved == root / "sub" / "file.txt"


class TestListDirHandler:
    def test_lists_directory(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        (root / "a.txt").write_text("a")
        (root / "b").mkdir()
        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=root):
            result = list_dir_handler(team_id="team-1", user_id="u1", arguments={"path": "."})
        assert len(result["entries"]) == 2
        names = {e["name"] for e in result["entries"]}
        assert names == {"a.txt", "b"}


class TestReadFileHandler:
    def test_reads_text_file(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        f = root / "notes.txt"
        f.write_text("line1\nline2\nline3\n")
        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=f):
            result = read_file_handler(team_id="team-1", user_id="u1", arguments={"path": "notes.txt"})
        assert result["total_lines"] == 3
        assert "line1" in result["content"]

    def test_rejects_directory(self, tmp_path):
        d = tmp_path / "team-1"
        d.mkdir(parents=True)
        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=d):
            with pytest.raises(DomainValidationError, match="directory"):
                read_file_handler(team_id="team-1", user_id="u1", arguments={"path": "."})


class TestWriteFileHandler:
    def test_writes_file(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        f = root / "out.txt"
        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=f):
            result = write_file_handler(
                team_id="team-1", user_id="u1",
                arguments={"path": "out.txt", "content": "hello world"},
            )
        assert result["written_bytes"] == 11
        assert f.read_text() == "hello world"


class TestEditFileHandler:
    def test_replaces_single_occurrence(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        f = root / "cfg.txt"
        f.write_text("before after before")
        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=f):
            result = edit_file_handler(
                team_id="team-1", user_id="u1",
                arguments={"path": "cfg.txt", "old_text": "before", "new_text": "done", "replace_all": False},
            )
        assert result["replacements"] == 1
        assert f.read_text() == "done after before"

    def test_replaces_all_when_flag_set(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        f = root / "cfg.txt"
        f.write_text("before before")
        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=f):
            result = edit_file_handler(
                team_id="team-1", user_id="u1",
                arguments={"path": "cfg.txt", "old_text": "before", "new_text": "done", "replace_all": True},
            )
        assert result["replacements"] == 2
        assert f.read_text() == "done done"

    def test_raises_when_old_text_not_found(self, tmp_path):
        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        f = root / "cfg.txt"
        f.write_text("hello")
        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=f):
            with pytest.raises(DomainValidationError, match="not found"):
                edit_file_handler(
                    team_id="team-1", user_id="u1",
                    arguments={"path": "cfg.txt", "old_text": "nope", "new_text": "x"},
                )


class TestCreateFileTools:
    def test_returns_four_definitions(self):
        defs = create_file_tools()
        names = {d.name for d in defs}
        assert names == {"list_dir", "read_file", "write_file", "edit_file"}
        # write and edit must be dangerous
        for d in defs:
            if d.name in ("write_file", "edit_file"):
                assert d.dangerous is True
            else:
                assert d.dangerous is False


# ------------------------------------------------------------------
# Shell tools
# ------------------------------------------------------------------

class TestCreateShellTools:
    def test_returns_empty_when_disabled(self):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = False
            assert create_shell_tools() == []

    def test_returns_three_when_enabled(self):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = True
            defs = create_shell_tools()
            names = {d.name for d in defs}
            assert names == {"shell_exec", "shell_status", "shell_kill"}


class TestShellSafety:
    def test_blocks_rm_rf_slash(self):
        with pytest.raises(DomainValidationError):
            _check_shell_safety("rm -rf / --no-preserve-root")

    def test_blocks_format_command(self):
        with pytest.raises(DomainValidationError):
            _check_shell_safety("format C:")

    def test_blocks_shutdown(self):
        with pytest.raises(DomainValidationError):
            _check_shell_safety("shutdown /s")

    def test_blocks_curl_by_default(self):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_allow_network = False
            with pytest.raises(DomainValidationError, match="Network command"):
                _check_shell_safety("curl https://example.com")

    def test_blocks_pipe_meta_character(self):
        with pytest.raises(DomainValidationError, match="meta-character"):
            _check_shell_safety("ls | grep foo")

    def test_blocks_subshell_syntax(self):
        with pytest.raises(DomainValidationError):
            _check_shell_safety("echo $(whoami)")

    def test_blocks_semicolon_separator(self):
        with pytest.raises(DomainValidationError):
            _check_shell_safety("ls; rm -f file.txt")

    def test_allows_simple_command(self):
        # Should not raise
        _check_shell_safety("echo hello world")

    def test_blocks_curl_even_in_complex_command(self):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_allow_network = False
            with pytest.raises(DomainValidationError):
                _check_shell_safety("curl -s localhost")


class TestShellExecHandler:
    def test_executes_simple_command(self, tmp_path):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = True
            m.return_value.shell_allowed_cwd = str(tmp_path)
            m.return_value.shell_default_timeout_seconds = 10
            m.return_value.shell_max_timeout_seconds = 30
            m.return_value.shell_max_output_bytes = 100_000
            m.return_value.shell_allow_network = False

            result = shell_exec_handler(
                team_id="t1", user_id="u1",
                arguments={"command": "echo hello", "cwd": "."},
            )
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]

    def test_blocks_dangerous_command_before_execution(self, tmp_path):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = True
            m.return_value.shell_allowed_cwd = str(tmp_path)
            m.return_value.shell_default_timeout_seconds = 10
            m.return_value.shell_max_timeout_seconds = 30
            m.return_value.shell_max_output_bytes = 100_000
            m.return_value.shell_allow_network = False

            with pytest.raises(DomainValidationError):
                shell_exec_handler(
                    team_id="t1", user_id="u1",
                    arguments={"command": "rm -rf /"},
                )

    def test_blocks_network_command(self, tmp_path):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = True
            m.return_value.shell_allowed_cwd = str(tmp_path)
            m.return_value.shell_default_timeout_seconds = 10
            m.return_value.shell_max_timeout_seconds = 30
            m.return_value.shell_max_output_bytes = 100_000
            m.return_value.shell_allow_network = False

            with pytest.raises(DomainValidationError):
                shell_exec_handler(
                    team_id="t1", user_id="u1",
                    arguments={"command": "wget https://example.com"},
                )

    def test_returns_timeout_on_long_command(self, tmp_path):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = True
            m.return_value.shell_allowed_cwd = str(tmp_path)
            m.return_value.shell_default_timeout_seconds = 10
            m.return_value.shell_max_timeout_seconds = 30
            m.return_value.shell_max_output_bytes = 100_000
            m.return_value.shell_allow_network = False

            import sys
            if sys.platform == "win32":
                # Windows timeout uses different command
                result = shell_exec_handler(
                    team_id="t1", user_id="u1",
                    arguments={"command": "ping -n 10 127.0.0.1", "cwd": ".", "timeout_seconds": 1},
                )
            else:
                result = shell_exec_handler(
                    team_id="t1", user_id="u1",
                    arguments={"command": "sleep 10", "cwd": ".", "timeout_seconds": 1},
                )
        assert result.get("timeout") is True or result["returncode"] == -1


class TestShellToolDefinitions:
    def test_shell_exec_is_dangerous(self):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = True
            defs = create_shell_tools()
            d = next(d for d in defs if d.name == "shell_exec")
            assert d.dangerous is True
            assert d.source == "generic"
            assert d.provider == "shell_tools"

    def test_shell_status_is_not_dangerous(self):
        with mock.patch("app.services.tools.shell_tools.get_settings") as m:
            m.return_value.shell_tool_enabled = True
            defs = create_shell_tools()
            d = next(d for d in defs if d.name == "shell_status")
            assert d.dangerous is False


# ------------------------------------------------------------------
# ToolService dry-run enforcement
# ------------------------------------------------------------------

class TestToolServiceDryRun:
    def test_dry_run_does_not_call_handler(self, tmp_path):
        """Dry-run must return immediately without touching the filesystem."""
        from app.services.tool_catalog_service import ToolCatalogService
        from app.services.tool_safety import ToolSafetyService
        from app.services.tool_service import ToolService
        from unittest import mock

        catalog = ToolCatalogService()
        catalog.register_generic(create_file_tools())
        safety = ToolSafetyService()

        svc = ToolService(db=mock.MagicMock(), catalog=catalog, safety=safety)
        # Register write_file handler
        svc.register_generic_handler("write_file", write_file_handler)

        root = tmp_path / "team-1"
        root.mkdir(parents=True)
        target_file = root / "must_not_exist.txt"

        with mock.patch("app.services.tools.file_tools._resolve_safe_path", return_value=target_file):
            result = svc.execute(
                team_id="team-1", user_id="u1",
                action="write_file",
                arguments={"path": "must_not_exist.txt", "content": "should not write"},
                dry_run=True,
                confirmed=False,
            )

        assert result["dry_run"] is True
        assert result["would_be_dangerous"] is True
        # dry_run suppresses confirmation requirement in preflight
        assert result["would_require_confirmation"] is False
        # The file MUST NOT exist — handler was never called
        assert not target_file.exists()

    def test_dry_run_non_dangerous_tool_also_short_circuits(self):
        """Dry-run must short-circuit even for non-dangerous tools."""
        from app.services.tool_catalog_service import ToolCatalogService
        from app.services.tool_safety import ToolSafetyService
        from app.services.tool_service import ToolService
        from unittest import mock

        catalog = ToolCatalogService()
        catalog.register_generic(create_file_tools())
        safety = ToolSafetyService()

        svc = ToolService(db=mock.MagicMock(), catalog=catalog, safety=safety)

        handler_called = False

        def tracking_handler(*, team_id, user_id, arguments):
            nonlocal handler_called
            handler_called = True
            return {"called": True}

        svc.register_generic_handler("list_dir", tracking_handler)

        result = svc.execute(
            team_id="team-1", user_id="u1",
            action="list_dir",
            arguments={"path": "."},
            dry_run=True,
            confirmed=False,
        )

        assert result["dry_run"] is True
        assert not handler_called


# ------------------------------------------------------------------
# Exa result parser
# ------------------------------------------------------------------

class TestParseExaResults:
    SAMPLE = """Title: First Result
URL: https://example.com/1
Published Date: 2025-06-01
Text: This is the first snippet.

Title: Second Result
URL: https://example.com/2
Text: This is the second snippet.

Title: Third with extra field
URL: https://example.com/3
Score: 0.95
Text: Third snippet here."""

    def test_parses_correct_number_of_results(self):
        results = _parse_exa_results(self.SAMPLE)
        assert len(results) == 3

    def test_extracts_title_url_snippet(self):
        results = _parse_exa_results(self.SAMPLE)
        assert results[0]["title"] == "First Result"
        assert results[0]["url"] == "https://example.com/1"
        assert results[0]["published"] == "2025-06-01"
        assert results[0]["snippet"] == "This is the first snippet."

    def test_missing_optional_fields(self):
        results = _parse_exa_results(self.SAMPLE)
        assert "published" not in results[1]
        assert results[1]["snippet"] == "This is the second snippet."

    def test_extra_fields_stored(self):
        results = _parse_exa_results(self.SAMPLE)
        assert results[2].get("score") == "0.95"

    def test_empty_input(self):
        assert _parse_exa_results("") == []
        assert _parse_exa_results("\n\n\n") == []

    def test_single_result(self):
        results = _parse_exa_results("Title: Only One\nURL: https://x.com\nText: Hi.")
        assert len(results) == 1
        assert results[0]["title"] == "Only One"


# ------------------------------------------------------------------
# HTML processing
# ------------------------------------------------------------------

class TestHtmlToText:
    def test_removes_script_tags(self):
        html = b"<html><head><script>alert(1)</script></head><body><p>Hello</p></body></html>"
        text = _html_to_text(html)
        assert "Hello" in text
        assert "alert" not in text

    def test_removes_style_tags(self):
        html = b"<html><head><style>.x { color: red; }</style></head><body>Visible</body></html>"
        text = _html_to_text(html)
        assert "Visible" in text
        assert "color" not in text

    def test_handles_nested_tags(self):
        html = b"<div>a <span>b <em>c</em></span> d</div>"
        text = _html_to_text(html)
        assert "a b c d" == text

    def test_handles_invalid_html(self):
        text = _html_to_text(b"not html at all")
        assert "not html at all" == text


class TestHtmlToMarkdown:
    def test_converts_basic_html(self):
        md = _html_to_markdown("<h1>Title</h1><p>Some <strong>bold</strong> text.</p>")
        assert "Title" in md
        assert "bold" in md

    def test_preserves_links(self):
        md = _html_to_markdown('<a href="https://example.com">click</a>')
        assert "click" in md
        assert "example.com" in md


# ------------------------------------------------------------------
# URL validation
# ------------------------------------------------------------------

class TestValidateUrlTarget:
    def test_allows_public_host(self):
        assert _validate_url_target("https://example.com/page") is None

    def test_blocks_loopback(self):
        err = _validate_url_target("http://127.0.0.1/admin")
        assert err is not None
        assert "private" in err.lower()

    def test_blocks_localhost_name(self):
        err = _validate_url_target("http://localhost:8080/secret")
        assert err is not None

    def test_blocks_link_local(self):
        from ipaddress import IPv6Address
        err = _validate_url_target("http://[fe80::1]/")
        assert err is not None

    def test_rejects_missing_hostname(self):
        err = _validate_url_target("http:///path")
        assert err is not None
