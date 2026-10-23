from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_agent_service, get_memory_service, require_current_active_user
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.db.session import get_db_session
from app.models.user import User
from app.schemas.companion_memory import (
    CompanionMemoryCreate, CompanionMemoryDocument, CompanionMemoryDocumentUpdate,
    CompanionMemoryItem, CompanionMemoryListResponse, CompanionMemoryUpdate, MemoryDocumentKind,
)
from app.services.agent_service import AgentService
from app.services.companion_memory_service import CompanionMemoryService
from app.services.memory_markdown_store import MemoryDocumentConflictError
from app.services.memory_service import MemoryService


router = APIRouter(prefix="/companion/memory")


def get_companion_memory_service(
    db: Session = Depends(get_db_session),
    memory_service: MemoryService = Depends(get_memory_service),
    agent_service: AgentService = Depends(get_agent_service),
) -> CompanionMemoryService:
    return CompanionMemoryService(db=db, memory_service=memory_service, agent_service=agent_service)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryDocumentConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, EntityNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("", response_model=CompanionMemoryListResponse)
def list_memories(
    conversation_id: str = Query(min_length=1, max_length=36),
    offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(require_current_active_user),
    service: CompanionMemoryService = Depends(get_companion_memory_service),
) -> CompanionMemoryListResponse:
    try:
        return service.list(conversation_id=conversation_id, team_id=current_user.team_id,
                            user_id=current_user.user_id, offset=offset, limit=limit)
    except (DomainValidationError, EntityNotFoundError) as exc:
        raise _error(exc) from exc


@router.post("/cards", response_model=CompanionMemoryItem, status_code=status.HTTP_201_CREATED)
def create_memory(
    payload: CompanionMemoryCreate, current_user: User = Depends(require_current_active_user),
    service: CompanionMemoryService = Depends(get_companion_memory_service),
) -> CompanionMemoryItem:
    try:
        return service.create(payload, team_id=current_user.team_id, user_id=current_user.user_id)
    except (DomainValidationError, EntityNotFoundError) as exc:
        raise _error(exc) from exc


@router.patch("/cards/{memory_id}", response_model=CompanionMemoryItem)
def update_memory(
    memory_id: str, payload: CompanionMemoryUpdate,
    current_user: User = Depends(require_current_active_user),
    service: CompanionMemoryService = Depends(get_companion_memory_service),
) -> CompanionMemoryItem:
    try:
        return service.update(memory_id, payload, team_id=current_user.team_id, user_id=current_user.user_id)
    except (DomainValidationError, EntityNotFoundError) as exc:
        raise _error(exc) from exc


@router.delete("/cards/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_memory(
    memory_id: str, conversation_id: str = Query(min_length=1, max_length=36),
    current_user: User = Depends(require_current_active_user),
    service: CompanionMemoryService = Depends(get_companion_memory_service),
) -> Response:
    try:
        service.delete(memory_id, conversation_id=conversation_id, team_id=current_user.team_id,
                       user_id=current_user.user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except (DomainValidationError, EntityNotFoundError) as exc:
        raise _error(exc) from exc


@router.put("/documents/{kind}", response_model=CompanionMemoryDocument)
def update_document(
    kind: MemoryDocumentKind, payload: CompanionMemoryDocumentUpdate,
    current_user: User = Depends(require_current_active_user),
    service: CompanionMemoryService = Depends(get_companion_memory_service),
) -> CompanionMemoryDocument:
    try:
        return service.update_document(kind, payload, team_id=current_user.team_id, user_id=current_user.user_id)
    except (DomainValidationError, EntityNotFoundError) as exc:
        raise _error(exc) from exc
