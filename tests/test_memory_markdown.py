from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.services.memory_markdown_store import MemoryMarkdownStore


@pytest.fixture
def store(tmp_path):
    s = MemoryMarkdownStore(root_dir=str(tmp_path))
    return s


@pytest.fixture
def ws(store):
    store.ensure_workspace("team-1", "user-1", "space-1")
    return store


class TestEnsureWorkspace:
    def test_creates_all_files(self, store):
        d = store.ensure_workspace("t1", "u1", "s1")
        assert d.exists()
        for name in ("SELF.md", "MEMORY.md", "HISTORY.md", "RECENT_CONTEXT.md", "PENDING.md"):
            assert (d / name).exists()
        assert (d / "journal").is_dir()

    def test_global_space_when_none(self, store):
        d = store.ensure_workspace("t1", "u1", None)
        assert d.name == "global"


class TestLongTermMemory:
    def test_write_and_read_roundtrip(self, ws):
        ws.write_long_term("team-1", "user-1", "space-1", "## Facts\n\n- I am a test user.")
        result = ws.read_long_term("team-1", "user-1", "space-1")
        assert "I am a test user" in result

    def test_read_empty_returns_empty_string(self, ws):
        result = ws.read_long_term("team-1", "user-1", "space-1")
        assert result == ""


class TestSelfModel:
    def test_default_self_model_is_created(self, ws):
        result = ws.read_self_model("team-1", "user-1", "space-1")
        assert "CaiBao 的自我认知" in result
        assert "人格与形象" in result

    def test_write_and_read_self_model_roundtrip(self, ws):
        ws.write_self_model("team-1", "user-1", "space-1", "# CaiBao 的自我认知\n\n- test")
        result = ws.read_self_model("team-1", "user-1", "space-1")
        assert result.endswith("- test")


class TestHistory:
    def test_append_and_read(self, ws):
        ws.append_history_once(
            "team-1", "user-1", "space-1",
            [{"summary": "User asked about database CPU."}],
            source_ref="ref-1",
        )
        text = ws.read_history("team-1", "user-1", "space-1")
        assert "User asked about database CPU" in text

    def test_idempotent_same_source_ref(self, ws):
        entries = [{"summary": "First turn."}]
        ok1 = ws.append_history_once("team-1", "user-1", "space-1", entries, "ref-1")
        ok2 = ws.append_history_once("team-1", "user-1", "space-1", entries, "ref-1")
        assert ok1 is True
        assert ok2 is False
        # Content appears only once
        text = ws.read_history("team-1", "user-1", "space-1")
        assert text.count("First turn") == 1

    def test_different_source_ref_appends(self, ws):
        ws.append_history_once("team-1", "user-1", "space-1",
                               [{"summary": "A."}], "ref-a")
        ws.append_history_once("team-1", "user-1", "space-1",
                               [{"summary": "B."}], "ref-b")
        text = ws.read_history("team-1", "user-1", "space-1")
        assert "A." in text
        assert "B." in text

    def test_max_chars_truncates(self, ws):
        long_summary = "X" * 100
        ws.append_history_once("team-1", "user-1", "space-1",
                               [{"summary": long_summary}], "ref-long")
        text = ws.read_history("team-1", "user-1", "space-1", max_chars=50)
        assert len(text) <= 55


class TestRecentContext:
    def test_write_and_read(self, ws):
        ws.write_recent_context("team-1", "user-1", "space-1",
                                "## Ongoing\n\nWorking on tests.\n\n<!-- BEGIN Recent Turns -->\n- turn 1\n<!-- END Recent Turns -->")
        result = ws.read_recent_context("team-1", "user-1", "space-1",
                                        include_recent_turns=False)
        assert "Working on tests" in result
        assert "BEGIN Recent Turns" not in result
        assert "END Recent Turns" not in result

    def test_include_recent_turns(self, ws):
        ws.write_recent_context("team-1", "user-1", "space-1",
                                "## Ongoing\n\nWorking.\n\n<!-- BEGIN Recent Turns -->\n- turn 1\n<!-- END Recent Turns -->")
        result = ws.read_recent_context("team-1", "user-1", "space-1",
                                        include_recent_turns=True)
        assert "BEGIN Recent Turns" in result
        assert "turn 1" in result


class TestPending:
    def test_append_and_read(self, ws):
        ws.append_pending_once(
            "team-1", "user-1", "space-1",
            [{"tag": "preference", "content": "User prefers short answers."}],
            source_ref="ref-p1",
        )
        text = ws.read_pending("team-1", "user-1", "space-1")
        assert "preference" in text
        assert "short answers" in text

    def test_idempotent_same_source_ref(self, ws):
        items = [{"tag": "key_info", "content": "Project deadline: Friday."}]
        ok1 = ws.append_pending_once("team-1", "user-1", "space-1", items, "ref-deadline")
        ok2 = ws.append_pending_once("team-1", "user-1", "space-1", items, "ref-deadline")
        assert ok1 is True
        assert ok2 is False
        text = ws.read_pending("team-1", "user-1", "space-1")
        assert text.count("deadline") == 1


class TestPendingSnapshot:
    def test_snapshot_commit_removes_snapshot(self, ws):
        ws.append_pending_once(
            "team-1", "user-1", "space-1",
            [{"tag": "identity", "content": "Name: Alex"}],
            source_ref="ref-id",
        )
        sid = ws.snapshot_pending("team-1", "user-1", "space-1")
        assert len(sid) == 12
        snap_dir = ws._workspace_dir("team-1", "user-1", "space-1") / ".snapshots"
        assert (snap_dir / f"{sid}.md").exists()
        ws.commit_pending_snapshot("team-1", "user-1", "space-1", sid)
        assert not (snap_dir / f"{sid}.md").exists()

    def test_snapshot_rollback_restores(self, ws):
        ws.append_pending_once(
            "team-1", "user-1", "space-1",
            [{"tag": "identity", "content": "Name: Alex"}],
            source_ref="ref-id-2",
        )
        original = ws.read_pending("team-1", "user-1", "space-1")
        sid = ws.snapshot_pending("team-1", "user-1", "space-1")

        # Clear PENDING.md
        f = ws._workspace_dir("team-1", "user-1", "space-1") / "PENDING.md"
        f.write_text("", encoding="utf-8")

        ws.rollback_pending_snapshot("team-1", "user-1", "space-1", sid)
        restored = ws.read_pending("team-1", "user-1", "space-1")
        assert restored == original


class TestAtomicWrite:
    def test_partial_write_does_not_corrupt(self, ws, tmp_path):
        """If the write fails mid-way, the original file is not corrupted."""
        f = ws._workspace_dir("team-1", "user-1", "space-1") / "MEMORY.md"
        f.write_text("original content", encoding="utf-8")

        # Simulate failure during atomic write by patching os.replace
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            try:
                ws.write_long_term("team-1", "user-1", "space-1", "new content")
            except OSError:
                pass

        # Original must be intact
        assert f.read_text("utf-8") == "original content"
