from __future__ import annotations

from unittest import mock
from uuid import uuid4

import pytest

from app.models.proactive_source import ProactiveSource


class TestProactiveEnergy:
    def test_high_severity_scores_high(self):
        from app.services.proactive_energy import ProactiveEnergy
        energy = ProactiveEnergy()
        score, should_push = energy.score(severity="critical", relevance=0.5)
        assert score >= 0.55  # 0.4*1.0 + 0.35*0.5 = 0.575

    def test_low_severity_scores_low(self):
        from app.services.proactive_energy import ProactiveEnergy
        energy = ProactiveEnergy()
        score, should_push = energy.score(severity="low", relevance=0.2)
        assert score < 0.6

    def test_fatigue_increases_with_repeated_pushes(self):
        from app.services.proactive_energy import ProactiveEnergy
        energy = ProactiveEnergy()
        for _ in range(5):
            energy.record_push()
        score1, _ = energy.score(severity="info", relevance=0.5)
        # Fatigue should reduce score
        energy2 = ProactiveEnergy()
        score2, _ = energy2.score(severity="info", relevance=0.5)
        assert score1 <= score2  # fatigued score ≤ fresh score

    def test_next_tick_increases_with_fatigue(self):
        from app.services.proactive_energy import ProactiveEnergy
        energy = ProactiveEnergy(base_interval=60, max_interval=480)
        t1 = energy.next_tick_seconds()
        for _ in range(10):
            energy.record_push()
        t2 = energy.next_tick_seconds()
        assert t2 >= t1


class TestProactiveGateway:
    def test_dedupe_hash_is_stable(self):
        from app.services.proactive_gateway import ProactiveGateway
        e = {"event_id": "evt1", "title": "Test"}
        h1 = ProactiveGateway.dedupe_hash(e)
        h2 = ProactiveGateway.dedupe_hash(e)
        assert h1 == h2

    def test_dedupe_hash_differs_for_different_events(self):
        from app.services.proactive_gateway import ProactiveGateway
        h1 = ProactiveGateway.dedupe_hash({"a": 1})
        h2 = ProactiveGateway.dedupe_hash({"b": 2})
        assert h1 != h2

    def test_pull_returns_empty_on_mcp_failure(self):
        from app.services.proactive_gateway import ProactiveGateway
        mock_mcp = mock.MagicMock()
        mock_mcp.call_tool.side_effect = RuntimeError("MCP down")
        gw = ProactiveGateway(mock_mcp)
        events = gw.pull_events({"mcp_server": "srv", "get_tool": "tool"})
        assert events == []


class TestProactiveService:
    def test_tick_with_no_sources(self):
        from app.services.proactive_gateway import ProactiveGateway
        from app.services.proactive_service import ProactiveService
        mock_db = mock.MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        gw = ProactiveGateway(mock.MagicMock())
        svc = ProactiveService(mock_db, gw)
        result = svc.run_tick()
        assert result["events_pulled"] == 0
        assert result["deliveries"] == 0
