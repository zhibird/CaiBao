from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_agent_app_service, require_current_active_user
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.user import User
from app.schemas.agent import AgentRunResponse
from app.schemas.agent_app import (
    AgentAppCreate,
    AgentAppInvokeRequest,
    AgentAppListResponse,
    AgentAppPublishRequest,
    AgentAppResponse,
    AgentAppUpdate,
    AgentAppVersionResponse,
)
from app.services.agent_app_service import AgentAppService

router = APIRouter(prefix="/apps")


@router.post("", response_model=AgentAppResponse, status_code=status.HTTP_201_CREATED)
def create_agent_app(
    payload: AgentAppCreate,
    current_user: User = Depends(require_current_active_user),
    agent_app_service: AgentAppService = Depends(get_agent_app_service),
) -> AgentAppResponse:
    try:
        item = agent_app_service.create(
            payload.model_copy(update={"team_id": current_user.team_id, "user_id": current_user.user_id})
        )
        return AgentAppResponse.from_record(item)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=AgentAppListResponse)
def list_agent_apps(
    current_user: User = Depends(require_current_active_user),
    agent_app_service: AgentAppService = Depends(get_agent_app_service),
) -> AgentAppListResponse:
    try:
        items = agent_app_service.list(team_id=current_user.team_id, user_id=current_user.user_id)
        return AgentAppListResponse(items=[AgentAppResponse.from_record(item) for item in items])
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/{app_id}", response_model=AgentAppResponse)
def get_agent_app(
    app_id: str,
    current_user: User = Depends(require_current_active_user),
    agent_app_service: AgentAppService = Depends(get_agent_app_service),
) -> AgentAppResponse:
    try:
        return AgentAppResponse.from_record(
            agent_app_service.get(app_id=app_id, team_id=current_user.team_id, user_id=current_user.user_id)
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch("/{app_id}", response_model=AgentAppResponse)
def update_agent_app(
    app_id: str,
    payload: AgentAppUpdate,
    current_user: User = Depends(require_current_active_user),
    agent_app_service: AgentAppService = Depends(get_agent_app_service),
) -> AgentAppResponse:
    try:
        item = agent_app_service.update(
            app_id=app_id,
            payload=payload.model_copy(update={"team_id": current_user.team_id, "user_id": current_user.user_id}),
        )
        return AgentAppResponse.from_record(item)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{app_id}/publish", response_model=AgentAppVersionResponse)
def publish_agent_app(
    app_id: str,
    payload: AgentAppPublishRequest,
    current_user: User = Depends(require_current_active_user),
    agent_app_service: AgentAppService = Depends(get_agent_app_service),
) -> AgentAppVersionResponse:
    try:
        version = agent_app_service.publish(
            app_id=app_id,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            notes=payload.notes,
        )
        return AgentAppVersionResponse.from_record(version)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{app_id}/invoke", response_model=AgentRunResponse)
def invoke_agent_app(
    app_id: str,
    payload: AgentAppInvokeRequest,
    current_user: User = Depends(require_current_active_user),
    agent_app_service: AgentAppService = Depends(get_agent_app_service),
) -> AgentRunResponse:
    try:
        return agent_app_service.invoke(
            app_id=app_id,
            payload=payload.model_copy(update={"team_id": current_user.team_id, "user_id": current_user.user_id}),
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
