from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_agent_service, require_current_active_user
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.user import User
from app.schemas.agent import (
    AgentConfirmRequest,
    AgentRunRequest,
    AgentRunResponse,
    AgentRunStartResponse,
    AgentRunListResponse,
    AgentRunEventsResponse,
    AgentToolDefinition,
)
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agent")


def _get_runtime():
    from app.api.deps import get_agent_run_runtime
    return get_agent_run_runtime()


@router.get("/runs", response_model=AgentRunListResponse)
def list_agent_runs(
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    trigger_channel: str | None = Query(default=None, min_length=1, max_length=32),
    status_filter: str | None = Query(default=None, alias="status", min_length=1, max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunListResponse:
    try:
        return AgentRunListResponse(items=agent_service.list_runs(
            team_id=current_user.team_id, user_id=current_user.user_id,
            conversation_id=conversation_id, trigger_channel=trigger_channel,
            status=status_filter, limit=limit,
        ))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
):
    """Observe durable events. A separate worker owns queued execution."""
    try:
        agent_service.get_run(
            run_id=run_id,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
        )
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative cursor.") from exc
            if int(last_event_id) < 0:
                raise HTTPException(status_code=400, detail="Last-Event-ID must be a nonnegative cursor.")
        if cursor > 9223372036854775807:
            raise HTTPException(status_code=400, detail="Event cursor is too large.")
        runtime = _get_runtime()
        runtime.ensure_started(run_id)

        def event_stream():
            # Closing this observer closes only this iterator, never the worker.
            for event in runtime.iter_events(run_id, after=cursor, heartbeat_seconds=15):
                if event is None:
                    yield ": heartbeat\n\n"
                    continue
                data_json = event.model_dump_json()
                yield f"id: {event.seq}\nevent: {event.event}\ndata: {data_json}\n\n"

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


@router.get("/runs/{run_id}/events", response_model=AgentRunEventsResponse)
def read_agent_run_events(
    run_id: str,
    after: int = Query(default=0, ge=0, le=9223372036854775807),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunEventsResponse:
    try:
        run = agent_service.get_run(run_id=run_id, team_id=current_user.team_id, user_id=current_user.user_id)
        events = _get_runtime().read_events(run_id, after=after, limit=limit + 1)
        page = events[:limit]
        return AgentRunEventsResponse(items=page, next_seq=page[-1].seq if page else after,
                                      status=run.status, has_more=len(events) > limit)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/start", response_model=AgentRunStartResponse)
def start_agent_run(
    run_id: str,
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunStartResponse:
    try:
        agent_service.get_run(run_id=run_id, team_id=current_user.team_id, user_id=current_user.user_id)
        _get_runtime().ensure_started(run_id)
        run = agent_service.get_run(run_id=run_id, team_id=current_user.team_id, user_id=current_user.user_id)
        return AgentRunStartResponse(run_id=run_id, stream_url=f"/agent/runs/{run_id}/stream", status=run.status)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/cancel", response_model=AgentRunResponse)
def cancel_agent_run(
    run_id: str,
    current_user: User = Depends(require_current_active_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentRunResponse:
    """Stop at the next model/tool boundary; active external work may finish."""
    try:
        response = agent_service.cancel_run(
            run_id=run_id, team_id=current_user.team_id, user_id=current_user.user_id,
        )
        _get_runtime().record_snapshot(response)
        return response
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
        # Authorize before handing execution to a worker with its own DB session.
        agent_service.get_run(run_id=run_id, team_id=current_user.team_id, user_id=current_user.user_id)
        return _get_runtime().execute_confirmation(
            run_id=run_id,
            team_id=effective_payload.team_id,
            user_id=effective_payload.user_id,
            approval_token=payload.approval_token,
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
