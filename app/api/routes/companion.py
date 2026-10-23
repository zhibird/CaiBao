from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_agent_service, require_current_active_user
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.db.session import get_db_session
from app.models.user import User
from app.schemas.agent import AgentRunStartResponse
from app.schemas.companion import (
    CompanionEventRequest,
    CompanionEventResponse,
    CompanionProfile,
    CompanionRunRequest,
)
from app.services.agent_service import AgentService
from app.services.companion_service import CompanionService

router = APIRouter(prefix="/companion")


def get_companion_service(
    db: Session = Depends(get_db_session),
    agent_service: AgentService = Depends(get_agent_service),
) -> CompanionService:
    return CompanionService(db=db, agent_service=agent_service)


@router.get("/profile", response_model=CompanionProfile)
def get_profile(
    current_user: User = Depends(require_current_active_user),
    service: CompanionService = Depends(get_companion_service),
) -> CompanionProfile:
    return service.profile(team_id=current_user.team_id, user_id=current_user.user_id)


@router.post("/runs", response_model=AgentRunStartResponse, status_code=status.HTTP_201_CREATED)
def create_run(
    payload: CompanionRunRequest,
    current_user: User = Depends(require_current_active_user),
    service: CompanionService = Depends(get_companion_service),
) -> AgentRunStartResponse:
    try:
        return service.start_run(payload, team_id=current_user.team_id, user_id=current_user.user_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/events", response_model=CompanionEventResponse, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: CompanionEventRequest,
    current_user: User = Depends(require_current_active_user),
    service: CompanionService = Depends(get_companion_service),
) -> CompanionEventResponse:
    return service.record_event(payload, team_id=current_user.team_id, user_id=current_user.user_id)
