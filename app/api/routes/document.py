from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from app.api.deps import get_chunk_service, get_document_service, require_current_active_user
from app.core.config import reload_settings
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.user import User
from app.schemas.document import DocumentImportRequest, DocumentResponse
from app.schemas.document_chunk import (
    DocumentChunkingRequest,
    DocumentChunkingResult,
    DocumentChunkResponse,
)
from app.db.session import SessionLocal
from app.services.chunk_service import ChunkService
from app.services.document_service import DocumentService
from app.services.embedding_model_service import EmbeddingModelService
from app.services.embedding_service import EmbeddingService
from app.services.retrieval_service import RetrievalService
from app.services.user_service import UserService

router = APIRouter(prefix="/documents")


@router.post("/import", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
def import_document(
    payload: DocumentImportRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    try:
        document = document_service.import_document(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            payload=payload,
        )
        if payload.auto_index:
            background_tasks.add_task(
                _process_document_pipeline_task,
                document_id=document.document_id,
                team_id=current_user.team_id,
                conversation_id=payload.conversation_id,
                user_id=current_user.user_id,
                auto_index=payload.auto_index,
                embedding_model=payload.embedding_model,
            )
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return DocumentResponse.model_validate(document)


@router.post("/upload", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    background_tasks: BackgroundTasks,
    conversation_id: str | None = Form(default=None),
    space_id: str | None = Form(default=None),
    auto_index: bool = Form(default=True),
    embedding_model: str | None = Form(default=None),
    file: UploadFile = File(...),
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    try:
        payload = await file.read()
        document = document_service.upload_document(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            conversation_id=conversation_id,
            space_id=space_id,
            source_name=file.filename or "uploaded.bin",
            declared_mime_type=file.content_type,
            file_bytes=payload,
        )
        if auto_index:
            background_tasks.add_task(
                _process_document_pipeline_task,
                document_id=document.document_id,
                team_id=current_user.team_id,
                conversation_id=conversation_id,
                user_id=current_user.user_id,
                auto_index=auto_index,
                embedding_model=embedding_model,
            )
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    finally:
        await file.close()

    return DocumentResponse.model_validate(document)


@router.get("", response_model=list[DocumentResponse])
def list_documents(
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    space_id: str | None = Query(default=None, min_length=1, max_length=36),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
) -> list[DocumentResponse]:
    try:
        documents = document_service.list_documents(
            team_id=current_user.team_id,
            conversation_id=conversation_id,
            space_id=space_id,
            user_id=current_user.user_id,
            status=status_filter,
            limit=limit,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return [DocumentResponse.model_validate(document) for document in documents]


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: str,
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    try:
        document = document_service.get_document_in_team(
            document_id=document_id,
            team_id=current_user.team_id,
            conversation_id=conversation_id,
            user_id=current_user.user_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return DocumentResponse.model_validate(document)


@router.get("/{document_id}/file")
def get_document_file(
    document_id: str,
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
) -> FileResponse:
    try:
        path, document = document_service.resolve_original_file(
            document_id=document_id,
            team_id=current_user.team_id,
            conversation_id=conversation_id,
            user_id=current_user.user_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return FileResponse(
        path=str(path),
        media_type=document.mime_type,
        filename=document.source_name,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: str,
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
) -> None:
    try:
        document_service.delete_document(
            document_id=document_id,
            team_id=current_user.team_id,
            conversation_id=conversation_id,
            user_id=current_user.user_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{document_id}/chunk", response_model=DocumentChunkingResult)
def chunk_document(
    document_id: str,
    payload: DocumentChunkingRequest,
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
    chunk_service: ChunkService = Depends(get_chunk_service),
) -> DocumentChunkingResult:
    try:
        document = document_service.get_document_in_team(
            document_id=document_id,
            team_id=current_user.team_id,
            conversation_id=payload.conversation_id,
            user_id=current_user.user_id,
        )
        chunks = chunk_service.chunk_document(
            document_id=document_id,
            team_id=current_user.team_id,
            conversation_id=document.conversation_id,
            max_chars=payload.max_chars,
            overlap=payload.overlap,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return DocumentChunkingResult(
        document_id=document_id,
        team_id=current_user.team_id,
        total_chunks=len(chunks),
        chunks=[DocumentChunkResponse.model_validate(chunk) for chunk in chunks],
    )


@router.get("/{document_id}/chunks", response_model=list[DocumentChunkResponse])
def list_chunks(
    document_id: str,
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    current_user: User = Depends(require_current_active_user),
    document_service: DocumentService = Depends(get_document_service),
    chunk_service: ChunkService = Depends(get_chunk_service),
) -> list[DocumentChunkResponse]:
    try:
        document = document_service.get_document_in_team(
            document_id=document_id,
            team_id=current_user.team_id,
            conversation_id=conversation_id,
            user_id=current_user.user_id,
        )
        chunks = chunk_service.list_chunks(
            document_id=document_id,
            team_id=current_user.team_id,
            conversation_id=document.conversation_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return [DocumentChunkResponse.model_validate(chunk) for chunk in chunks]


def _process_document_pipeline_task(
    *,
    document_id: str,
    team_id: str,
    conversation_id: str | None,
    user_id: str | None,
    auto_index: bool,
    embedding_model: str | None,
) -> None:
    db = SessionLocal()
    try:
        document_service = DocumentService(db)
        chunk_service = ChunkService(db)
        user_service = UserService(db)
        embedding_service = EmbeddingService(settings=reload_settings())
        embedding_model_service = EmbeddingModelService(db=db, user_service=user_service)
        retrieval_service = RetrievalService(
            db=db,
            embedding_service=embedding_service,
            embedding_model_service=embedding_model_service,
        )
        document_service.process_document_pipeline(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
            user_id=user_id,
            auto_index=auto_index,
            embedding_model=embedding_model,
            chunk_service=chunk_service,
            retrieval_service=retrieval_service,
        )
    finally:
        db.close()
