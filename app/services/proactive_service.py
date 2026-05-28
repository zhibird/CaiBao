from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.proactive_event_log import ProactiveEventLog
from app.models.proactive_source import ProactiveSource
from app.services.outbound_service import OutboundService
from app.services.proactive_energy import ProactiveEnergy
from app.services.proactive_gateway import ProactiveGateway

_logger = logging.getLogger(__name__)


class ProactiveService:
    """Orchestrates one tick: pull → classify → score → push → record."""

    def __init__(self, db: Session, gateway: ProactiveGateway) -> None:
        self.db = db
        self.gateway = gateway
        self.settings = get_settings()
        self.energy = ProactiveEnergy(
            urgency_weight=self.settings.proactive_energy_urgency_weight,
            relevance_weight=self.settings.proactive_energy_relevance_weight,
            fatigue_weight=self.settings.proactive_energy_fatigue_weight,
        )

    def run_tick(self, *, team_id: str = "") -> dict:
        """Run one proactive tick cycle. Returns summary dict."""
        q = self.db.query(ProactiveSource).filter(ProactiveSource.enabled.is_(True))
        if team_id:
            q = q.filter(ProactiveSource.team_id == team_id)
        sources = q.all()
        if not sources:
            return {"tick": "no_sources", "events_pulled": 0, "deliveries": 0}

        total_pulled = 0
        total_deliveries = 0

        for source in sources:
            src_dict = {
                "mcp_server": source.mcp_server,
                "get_tool": source.get_tool,
                "ack_tool": source.ack_tool,
                "channel": source.channel,
            }
            events = self.gateway.pull_events(src_dict)
            total_pulled += len(events)

            for event in events:
                event_hash = self.gateway.dedupe_hash(event)
                if self._is_duplicate_event(source.team_id, source.name, str(event.get("event_id", "")), event_hash):
                    continue

                self._log_event(source, event, event_hash)

                severity = str(event.get("severity", "")).lower()
                relevance = self._classify_relevance(event, source.channel)
                score, should_push = self.energy.score(severity=severity, relevance=relevance)

                # High severity always pushes
                if severity in ("critical", "p1", "high"):
                    should_push = True

                if should_push:
                    self._deliver(source, event, score)
                    total_deliveries += 1

        self.energy.record_push()
        return {
            "tick": "completed",
            "events_pulled": total_pulled,
            "deliveries": total_deliveries,
            "next_tick_seconds": self.energy.next_tick_seconds(),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_duplicate_event(self, team_id: str, source_name: str, event_id: str, event_hash: str) -> bool:
        q = self.db.query(ProactiveEventLog).filter(
            ProactiveEventLog.team_id == team_id,
            ProactiveEventLog.source_name == source_name,
        )
        if event_id:
            q = q.filter(ProactiveEventLog.event_id == event_id)
        else:
            q = q.filter(ProactiveEventLog.event_id == event_hash)
        return q.first() is not None

    @staticmethod
    def _classify_relevance(event: dict, channel: str) -> float:
        """Simple keyword-based relevance classification."""
        content = str(event.get("content", "") or event.get("title", "")).lower()
        if channel == "alert":
            return 0.9  # alerts are always relevant
        keywords = ["error", "failure", "outage", "incident", "告警", "故障"]
        for kw in keywords:
            if kw in content:
                return 0.8
        return 0.4

    def _log_event(self, source: ProactiveSource, event: dict, event_hash: str) -> None:
        import json
        log = ProactiveEventLog(
            log_id=str(uuid4()),
            team_id=source.team_id,
            source_name=source.name,
            event_id=str(event.get("event_id", event_hash)),
            channel=source.channel,
            title=str(event.get("title", ""))[:512] or None,
            content=str(event.get("content", ""))[:4000] or None,
            url=str(event.get("url", ""))[:2048] or None,
            status="new",
            classification=str(event.get("classification", ""))[:64] or None,
            raw_json=json.dumps(event, ensure_ascii=False, default=str),
        )
        self.db.add(log)
        self.db.commit()

    def _deliver(self, source: ProactiveSource, event: dict, score: float) -> None:
        outbound = OutboundService(self.db)
        msg = f"[{source.channel.upper()}] {event.get('title', event.get('content', 'Proactive event'))}"
        event_hash = self.gateway.dedupe_hash(event)
        outbound.send(
            team_id=source.team_id,
            user_id=source.user_id,
            channel=source.channel,
            message=msg,
            evidence=event,
            dedupe_hash=event_hash,
        )
