from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_mcp_manager, require_current_active_user
from app.core.exceptions import DomainValidationError
from app.db.session import get_db_session
from app.models.proactive_delivery import ProactiveDelivery
from app.models.proactive_source import ProactiveSource
from app.models.user import User
from app.services.mcp_manager import MCPManager
from app.services.outbound_service import OutboundService
from app.services.proactive_gateway import ProactiveGateway
from app.services.proactive_service import ProactiveService

from app.core.config import get_settings

router = APIRouter(prefix="/proactive")


def _check_proactive_enabled():
    if not get_settings().proactive_enabled:
        raise HTTPException(status_code=403, detail="Proactive features are disabled. Set PROACTIVE_ENABLED=true.")


@router.get("/sources")
def list_sources(
    current_user: User = Depends(require_current_active_user),
    db: Session = Depends(get_db_session),
):
    _check_proactive_enabled()
    sources = db.query(ProactiveSource).filter(
        ProactiveSource.team_id == current_user.team_id,
    ).all()
    return [
        {
            "source_id": s.source_id,
            "name": s.name,
            "channel": s.channel,
            "mcp_server": s.mcp_server,
            "get_tool": s.get_tool,
            "enabled": s.enabled,
            "created_at": str(s.created_at) if s.created_at else None,
        }
        for s in sources
    ]


@router.post("/sources")
def create_source(
    body: dict,
    current_user: User = Depends(require_current_active_user),
    db: Session = Depends(get_db_session),
):
    _check_proactive_enabled()
    try:
        source = ProactiveSource(
            source_id=str(uuid4()),
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            name=str(body.get("name", "")).strip(),
            channel=str(body.get("channel", "content")).strip(),
            mcp_server=str(body.get("mcp_server", "")).strip(),
            get_tool=str(body.get("get_tool", "")).strip(),
            ack_tool=str(body.get("ack_tool", "")).strip() or None,
            poll_tool=str(body.get("poll_tool", "")).strip() or None,
            enabled=bool(body.get("enabled", True)),
        )
        if not source.name or not source.mcp_server or not source.get_tool:
            raise DomainValidationError("name, mcp_server, and get_tool are required")
        db.add(source)
        db.commit()
        db.refresh(source)
        return {"source_id": source.source_id, "name": source.name, "channel": source.channel}
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/tick")
def run_tick(
    current_user: User = Depends(require_current_active_user),
    db: Session = Depends(get_db_session),
    mcp_manager: MCPManager = Depends(get_mcp_manager),
):
    _check_proactive_enabled()
    try:
        gateway = ProactiveGateway(mcp_manager)
        svc = ProactiveService(db, gateway)
        summary = svc.run_tick(team_id=current_user.team_id)
        return summary
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/deliveries")
def list_deliveries(
    current_user: User = Depends(require_current_active_user),
    db: Session = Depends(get_db_session),
):
    _check_proactive_enabled()
    deliveries = db.query(ProactiveDelivery).filter(
        ProactiveDelivery.team_id == current_user.team_id,
    ).order_by(ProactiveDelivery.created_at.desc()).limit(50).all()
    return [
        {
            "delivery_id": d.delivery_id,
            "channel": d.channel,
            "message": d.message[:200],
            "status": d.status,
            "retry_count": d.retry_count,
            "created_at": str(d.created_at) if d.created_at else None,
        }
        for d in deliveries
    ]


@router.post("/deliveries/{delivery_id}/retry")
def retry_delivery(
    delivery_id: str,
    current_user: User = Depends(require_current_active_user),
    db: Session = Depends(get_db_session),
):
    _check_proactive_enabled()
    svc = OutboundService(db)
    result = svc.retry(delivery_id, team_id=current_user.team_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found or not retriable")
    return {"delivery_id": result.delivery_id, "status": result.status, "retry_count": result.retry_count}
