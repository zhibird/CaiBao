from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_agent_service, require_current_active_user
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.user import User
from app.schemas.agent import (
    AgentConfirmRequest,
    AgentRunRequest,
    AgentRunResponse,
    AgentRunStartResponse,
    AgentToolDefinition,
)
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agent")


@router.post("/run", response_model=AgentRunResponse)
def run_agent(
    payload: AgentRunRequest,
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunResponse:
    """Synchronous agent run (backward-compatible)."""
    try:
        effective_payload = payload.model_copy(
            update={"team_id": current_user.team_id, "user_id": current_user.user_id}
        )
        return agent_service.run(effective_payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/runs", response_model=AgentRunStartResponse)
def create_queued_run(
    payload: AgentRunRequest,
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunStartResponse:
    """Create a queued agent run for streaming consumption."""
    try:
        effective_payload = payload.model_copy(
            update={"team_id": current_user.team_id, "user_id": current_user.user_id}
        )
        return agent_service.start(effective_payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
def get_agent_run(
    run_id: str,
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunResponse:
    try:
        return agent_service.get_run(
            run_id=run_id,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/runs/{run_id}/stream")
def stream_agent_run(
    run_id: str,
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
):
    """SSE stream: if queued, executes; if completed, replays steps."""
    try:
        agent_service.get_run(
            run_id=run_id,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
        )

        def event_stream():
            for event in agent_service.stream_run(run_id):
                data_json = event.model_dump_json()
                yield f"event: {event.event}\ndata: {data_json}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/runs/{run_id}/confirm", response_model=AgentRunResponse)
def confirm_agent_run(
    run_id: str,
    payload: AgentConfirmRequest,
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunResponse:
    try:
        effective_payload = payload.model_copy(
            update={"team_id": current_user.team_id, "user_id": current_user.user_id}
        )
        return agent_service.confirm_run(
            run_id=run_id,
            team_id=effective_payload.team_id,
            user_id=effective_payload.user_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/tools", response_model=list[AgentToolDefinition])
def list_agent_tools(
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> list[AgentToolDefinition]:
    try:
        agent_service.user_service.ensure_user_in_team(
            user_id=current_user.user_id,
            team_id=current_user.team_id,
        )
        return [AgentToolDefinition.model_validate(item) for item in agent_service.list_tools()]
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
