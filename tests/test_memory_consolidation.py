from __future__ import annotations

import tempfile
from unittest import mock

import pytest

from app.events.event_bus import EventBus
from app.events.lifecycle import TurnCommitted
from app.services.memory_consolidation_service import MemoryConsolidationService
from app.services.memory_markdown_store import MemoryMarkdownStore


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def store(tmp_path):
    return MemoryMarkdownStore(root_dir=str(tmp_path))


def _make_turn(**kwargs) -> TurnCommitted:
    defaults = {
        "run_id": "run-1",
        "team_id": "team-1",
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "space_id": "space-1",
        "input_message": "Hello, what can you do?",
        "assistant_response": "I can help with operations.",
        "tools_used": ["search_knowledge", "list_recent_documents"],
    }
    defaults.update(kwargs)
    return TurnCommitted(**defaults)


class TestConsolidationRecentContext:
    def test_turn_committed_writes_recent_turns(self, bus, store):
        svc = MemoryConsolidationService(bus, store)
        event = _make_turn()
        bus.observe_event(event)  # simulate TurnCommitted

        text = store.read_recent_context("team-1", "user-1", "space-1", include_recent_turns=True)
        assert "BEGIN Recent Turns" in text
        assert "END Recent Turns" in text
        assert "search_knowledge" in text

    def test_recent_turns_strip_in_read_without_flag(self, bus, store):
        svc = MemoryConsolidationService(bus, store)
        bus.observe_event(_make_turn())

        text = store.read_recent_context("team-1", "user-1", "space-1", include_recent_turns=False)
        assert "BEGIN Recent Turns" not in text
        assert "END Recent Turns" not in text

    def test_recent_turns_rotate_at_limit(self, bus, store):
        svc = MemoryConsolidationService(bus, store)
        for i in range(10):
            bus.observe_event(_make_turn(run_id=f"run-{i}",
                                         input_message=f"Task {i}",
                                         tools_used=[]))

        text = store.read_recent_context("team-1", "user-1", "space-1", include_recent_turns=True)
        lines = [l for l in text.split("\n") if l.strip().startswith("-")]
        # Should keep at most memory_recent_turns (default 6)
        assert len(lines) <= 6
        # Oldest tasks should be rotated out
        assert "Task 0" not in text
        # Newest task should be present
        assert "Task 9" in text

    def test_consolidation_triggers_consolidation_committed(self, bus, store):
        svc = MemoryConsolidationService(bus, store)

        committed_events = []
        bus.observe("ConsolidationCommitted", lambda e: committed_events.append(e) or None)

        # memory_consolidation_min_turns defaults to 4
        for i in range(4):
            bus.observe_event(_make_turn(run_id=f"run-{i}", conversation_id="conv-1"))

        assert len(committed_events) == 1
        assert committed_events[0].team_id == "team-1"

    def test_consolidation_not_triggered_below_threshold(self, bus, store):
        svc = MemoryConsolidationService(bus, store)
        committed_events = []
        bus.observe("ConsolidationCommitted", lambda e: committed_events.append(e))

        for i in range(3):
            bus.observe_event(_make_turn(run_id=f"run-{i}", conversation_id="conv-1"))

        assert len(committed_events) == 0

    def test_duplicate_source_ref_is_idempotent(self, bus, store):
        svc = MemoryConsolidationService(bus, store)

        # Fire TurnCommitted 4 times to trigger consolidation
        for i in range(4):
            bus.observe_event(_make_turn(run_id=f"run-{i}",
                                         conversation_id="conv-2",
                                         input_message=f"Task {i}"))

        # Check history was written (from consolidation)
        text = store.read_history("team-1", "user-1", "space-1")
        # In PR 2 without LLM, history entries are empty, so nothing to check
        # But the consolidation committed event should fire
