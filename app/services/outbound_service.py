from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.proactive_delivery import ProactiveDelivery

_logger = logging.getLogger(__name__)


class OutboundService:
    """Delivers proactive messages via configured channels."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def send(
        self,
        *,
        team_id: str,
        user_id: str,
        channel: str,
        message: str,
        target: str = "",
        evidence: dict[str, Any] | None = None,
        dedupe_hash: str = "",
        run_id: str = "",
    ) -> ProactiveDelivery:
        delivery = ProactiveDelivery(
            delivery_id=str(uuid4()),
            run_id=run_id or None,
            team_id=team_id,
            user_id=user_id,
            channel=channel,
            target=target or None,
            message=message,
            evidence_json=_to_json(evidence) if evidence else None,
            dedupe_hash=dedupe_hash,
            status="pending",
            retry_count=0,
        )

        channels = [c.strip().lower() for c in self.settings.proactive_outbound_channels.split(",") if c.strip()]

        if "webhook" in channels and self.settings.proactive_webhook_url:
            try:
                self._send_webhook(delivery)
                delivery.status = "sent"
            except Exception:
                _logger.exception("Webhook delivery failed for %s", delivery.delivery_id)
                delivery.status = "failed"
                delivery.retry_count = 1
        else:
            delivery.status = "sent"  # database-only mode

        self.db.add(delivery)
        self.db.commit()
        self.db.refresh(delivery)
        return delivery

    def retry(self, delivery_id: str, *, team_id: str = "") -> ProactiveDelivery | None:
        delivery = self.db.get(ProactiveDelivery, delivery_id)
        if delivery is None or delivery.status != "failed":
            return None
        if team_id and delivery.team_id != team_id:
            return None  # tenant isolation
        if delivery.retry_count >= self.settings.proactive_max_retries:
            return delivery
        try:
            self._send_webhook(delivery)
            delivery.status = "sent"
        except Exception:
            delivery.retry_count += 1
        self.db.add(delivery)
        self.db.commit()
        self.db.refresh(delivery)
        return delivery

    def _send_webhook(self, delivery: ProactiveDelivery) -> None:
        httpx.post(
            self.settings.proactive_webhook_url,
            json={
                "delivery_id": delivery.delivery_id,
                "team_id": delivery.team_id,
                "user_id": delivery.user_id,
                "channel": delivery.channel,
                "message": delivery.message,
                "evidence": delivery.evidence_json,
            },
            timeout=10,
        ).raise_for_status()


def _to_json(obj: dict[str, Any]) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
