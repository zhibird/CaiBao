from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, get_settings
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.chunk_embedding import ChunkEmbedding
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.project_space import ProjectSpace
from app.models.team import Team
from app.schemas.document import DocumentImportRequest
from app.services.chunk_service import ChunkSection, ChunkService
from app.services.retrieval_service import RetrievalService

try:
    from PIL import Image  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    Image = None

try:
    import pytesseract  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    pytesseract = None


@dataclass(frozen=True)
class ParseResult:
    text: str
    sections: list[ChunkSection]
    page_count: int | None
    meta: dict[str, object]


@dataclass(frozen=True)
class PipelineError:
    stage: str
    code: str
    message: str


class DocumentService:
    _ALLOWED_TYPES = {"txt", "md", "pdf", "png", "jpg", "jpeg", "webp", "docx", "xlsx"}
    _IMAGE_TYPES = {"png", "jpg", "jpeg", "webp"}
    _MIME_BY_TYPE = {
        "txt": "text/plain",
        "md": "text/markdown",
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }
    _MIME_ALIASES = {
        "txt": {"text/plain"},
        "md": {"text/markdown", "text/plain"},
        "pdf": {"application/pdf"},
        "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        "png": {"image/png"},
        "jpg": {"image/jpeg", "image/jpg"},
        "jpeg": {"image/jpeg", "image/jpg"},
        "webp": {"image/webp"},
    }
    _WORDPROCESSING_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    _SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    _OFFICE_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    _PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.upload_root = self._resolve_upload_root()

    def import_document(
        self,
        *,
        team_id: str,
        user_id: str | None,
        payload: DocumentImportRequest,
    ) -> Document:
        self._ensure_team_and_conversation(
            team_id=team_id,
            conversation_id=payload.conversation_id,
        )
        space_id = self.resolve_space_id(
            team_id=team_id,
            conversation_id=payload.conversation_id,
            space_id=payload.space_id,
            user_id=user_id,
        )

        now = datetime.now(timezone.utc)
        content = payload.content.strip()
        size_bytes = len(content.encode("utf-8"))
        if size_bytes <= 0:
            raise DomainValidationError("EMPTY_FILE: imported text content cannot be empty.")

        document = Document(
            document_id=str(uuid4()),
            team_id=team_id,
            conversation_id=payload.conversation_id,
            space_id=space_id,
            source_name=payload.source_name,
            content_type=payload.content_type,
            mime_type=self._MIME_BY_TYPE[payload.content_type],
            size_bytes=size_bytes,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            storage_key=f"inline://import/{uuid4()}",
            meta_json=json.dumps(payload.meta, ensure_ascii=False, separators=(",", ":"))
            if payload.meta
            else None,
            visibility="conversation" if payload.conversation_id else "space",
            asset_kind="attachment" if payload.conversation_id else "knowledge_doc",
            retrieval_enabled=True,
            origin_document_id=None,
            status="uploaded",
            content=content,
            created_at=now,
            updated_at=now,
        )
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document

    def upload_document(
        self,
        *,
        team_id: str,
        user_id: str | None,
        conversation_id: str | None,
        space_id: str | None,
        source_name: str,
        declared_mime_type: str | None,
        file_bytes: bytes,
    ) -> Document:
        self._ensure_team_and_conversation(team_id=team_id, conversation_id=conversation_id)
        effective_space_id = self.resolve_space_id(
            team_id=team_id,
            conversation_id=conversation_id,
            space_id=space_id,
            user_id=user_id,
        )
        if not file_bytes:
            raise DomainValidationError("EMPTY_FILE: uploaded file is empty.")

        max_size = max(1, int(self.settings.upload_max_file_size_mb)) * 1024 * 1024
        if len(file_bytes) > max_size:
            raise DomainValidationError(
                f"FILE_TOO_LARGE: file exceeds {self.settings.upload_max_file_size_mb} MB limit."
            )

        content_type = self._detect_content_type(source_name=source_name)
        mime_type = self._sniff_mime(content_type=content_type, file_bytes=file_bytes)
        self._validate_declared_mime(
            content_type=content_type,
            declared_mime_type=declared_mime_type,
            sniffed_mime_type=mime_type,
        )

        document_id = str(uuid4())
        storage_key = self._build_storage_key(
            team_id=team_id,
            document_id=document_id,
            source_name=source_name,
        )
        self._write_file(storage_key=storage_key, file_bytes=file_bytes)

        now = datetime.now(timezone.utc)
        document = Document(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
            space_id=effective_space_id,
            source_name=source_name,
            content_type=content_type,
            mime_type=mime_type,
            size_bytes=len(file_bytes),
            sha256=hashlib.sha256(file_bytes).hexdigest(),
            storage_key=storage_key,
            visibility="conversation" if conversation_id else "space",
            asset_kind="attachment" if conversation_id else "knowledge_doc",
            retrieval_enabled=True,
            origin_document_id=None,
            status="uploaded",
            content="",
            created_at=now,
            updated_at=now,
        )
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document

    def process_document_pipeline(
        self,
        *,
        document_id: str,
        team_id: str,
        conversation_id: str | None,
        user_id: str | None,
        auto_index: bool,
        embedding_model: str | None,
        chunk_service: ChunkService,
        retrieval_service: RetrievalService,
        max_chars: int = 600,
        overlap: int = 80,
    ) -> None:
        document = self.get_document_in_team(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        self._clear_failure(document)

        parse_result, parse_error = self._parse_phase(document)
        if parse_error is not None:
            self._mark_failed(document=document, error=parse_error)
            return

        _, chunk_error = self._chunk_phase(
            document=document,
            team_id=team_id,
            conversation_id=conversation_id,
            max_chars=max_chars,
            overlap=overlap,
            sections=parse_result.sections if parse_result is not None else None,
            chunk_service=chunk_service,
        )
        if chunk_error is not None:
            self._mark_failed(document=document, error=chunk_error)
            return

        if not auto_index:
            return

        index_error = self._index_phase(
            team_id=team_id,
            user_id=user_id,
            conversation_id=conversation_id,
            document_id=document_id,
            embedding_model=embedding_model,
            retrieval_service=retrieval_service,
        )
        if index_error is not None:
            document = self.get_document_in_team(
                document_id=document_id,
                team_id=team_id,
                conversation_id=conversation_id,
                user_id=user_id,
            )
            self._mark_failed(document=document, error=index_error)

    def list_documents(
        self,
        team_id: str,
        conversation_id: str | None = None,
        space_id: str | None = None,
        user_id: str | None = None,
        visibility: str | None = None,
        asset_kind: str | None = None,
        status: str | None = None,
        retrieval_enabled: bool | None = None,
        limit: int = 50,
    ) -> list[Document]:
        stmt = select(Document).where(Document.team_id == team_id)
        if conversation_id is not None and user_id is not None:
            self._ensure_conversation_access(team_id=team_id, conversation_id=conversation_id, user_id=user_id)
        if space_id is not None and user_id is not None:
            self.resolve_space_id(
                team_id=team_id,
                conversation_id=conversation_id,
                space_id=space_id,
                user_id=user_id,
            )
        if conversation_id is not None:
            stmt = stmt.where(Document.conversation_id == conversation_id)
        if space_id is not None:
            stmt = stmt.where(Document.space_id == space_id)
        if user_id is not None:
            stmt = (
                stmt.outerjoin(
                    Conversation,
                    and_(
                        Conversation.conversation_id == Document.conversation_id,
                        Conversation.team_id == Document.team_id,
                    ),
                )
                .outerjoin(
                    ProjectSpace,
                    and_(
                        ProjectSpace.space_id == Document.space_id,
                        ProjectSpace.team_id == Document.team_id,
                    ),
                )
                .where(
                    or_(
                        Conversation.user_id == user_id,
                        and_(
                            Document.conversation_id.is_(None),
                            Document.space_id.isnot(None),
                            ProjectSpace.owner_user_id == user_id,
                        ),
                        and_(
                            Document.conversation_id.is_(None),
                            Document.space_id.is_(None),
                        ),
                    )
                )
            )
        if visibility is not None:
            stmt = stmt.where(Document.visibility == visibility.strip().lower())
        if asset_kind is not None:
            stmt = stmt.where(Document.asset_kind == asset_kind.strip().lower())
        if status is not None:
            stmt = stmt.where(Document.status == status.strip().lower())
        if retrieval_enabled is not None:
            stmt = stmt.where(Document.retrieval_enabled.is_(retrieval_enabled))
        stmt = stmt.order_by(Document.created_at.desc())
        stmt = stmt.limit(max(1, min(limit, 200)))
        return list(self.db.scalars(stmt).all())

    def get_documents_in_scope(
        self,
        *,
        team_id: str,
        conversation_id: str | None,
        space_id: str | None,
        user_id: str | None = None,
        document_ids: list[str] | None = None,
        include_library: bool = False,
        include_conclusions: bool = False,
        ready_only: bool = True,
        limit: int = 200,
    ) -> list[Document]:
        if document_ids:
            ordered: list[Document] = []
            for document_id in document_ids:
                try:
                    item = self.get_document_in_team(
                        document_id=document_id,
                        team_id=team_id,
                        user_id=user_id,
                    )
                except EntityNotFoundError:
                    continue
                if ready_only and item.status != "ready":
                    continue
                if conversation_id is not None and item.conversation_id == conversation_id:
                    ordered.append(item)
                    continue
                if include_library and item.visibility in {"space", "global"}:
                    if item.visibility == "space" and space_id is not None and item.space_id != space_id:
                        continue
                    if not item.retrieval_enabled:
                        continue
                    if item.asset_kind == "conclusion_note" and not include_conclusions:
                        continue
                    ordered.append(item)
                    continue
                if conversation_id is None:
                    ordered.append(item)
                    continue
            return ordered

        items: list[Document] = []
        if conversation_id is not None:
            items.extend(
                self.list_documents(
                    team_id=team_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    status="ready" if ready_only else None,
                    limit=limit,
                )
            )
        if include_library and space_id is not None:
            items.extend(
                self.list_documents(
                    team_id=team_id,
                    space_id=space_id,
                    user_id=user_id,
                    visibility="space",
                    status="ready" if ready_only else None,
                    retrieval_enabled=True,
                    limit=limit,
                )
            )
        if include_conclusions and space_id is not None:
            items.extend(
                self.list_documents(
                    team_id=team_id,
                    space_id=space_id,
                    user_id=user_id,
                    visibility="space",
                    asset_kind="conclusion_note",
                    status="ready" if ready_only else None,
                    retrieval_enabled=True,
                    limit=limit,
                )
            )

        deduped: list[Document] = []
        seen: set[str] = set()
        for item in items:
            if ready_only and item.status != "ready":
                continue
            if item.asset_kind == "conclusion_note" and not include_conclusions:
                continue
            if item.document_id in seen:
                continue
            seen.add(item.document_id)
            deduped.append(item)
        return deduped

    def resolve_space_id(
        self,
        *,
        team_id: str,
        conversation_id: str | None,
        space_id: str | None,
        user_id: str | None = None,
    ) -> str | None:
        conversation: Conversation | None = None
        if conversation_id is not None:
            conversation = self.db.get(Conversation, conversation_id)
            if conversation is None:
                raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
            if conversation.team_id != team_id:
                raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
            if user_id is not None and conversation.user_id != user_id:
                raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
            if space_id is not None and conversation.space_id and conversation.space_id != space_id:
                raise DomainValidationError("space_id does not match the conversation's space.")

        target_space_id = space_id or (conversation.space_id if conversation is not None else None)
        if target_space_id is None:
            return None

        target_space = self.db.get(ProjectSpace, target_space_id)
        if target_space is None or target_space.status == "deleted":
            raise EntityNotFoundError(f"Space '{target_space_id}' not found.")
        if target_space.team_id != team_id:
            raise EntityNotFoundError(f"Space '{target_space_id}' not found.")
        if user_id is not None and target_space.owner_user_id != user_id:
            raise EntityNotFoundError(f"Space '{target_space_id}' not found.")
        return target_space.space_id

    def build_chat_image_attachments(
        self,
        *,
        documents: list[Document],
    ) -> list[dict[str, str]]:
        attachments: list[dict[str, str]] = []
        for item in documents:
            if item.content_type not in self._IMAGE_TYPES:
                continue
            if item.storage_key.startswith("inline://"):
                continue
            path = self.upload_root / item.storage_key
            if not path.exists() or not path.is_file():
                continue
            file_bytes = path.read_bytes()
            data_url = self._to_data_url(mime_type=item.mime_type, file_bytes=file_bytes)
            attachments.append(
                {
                    "document_id": item.document_id,
                    "source_name": item.source_name,
                    "mime_type": item.mime_type,
                    "data_url": data_url,
                }
            )
        return attachments

    def get_document_in_team(
        self,
        document_id: str,
        team_id: str,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> Document:
        stmt = select(Document).where(
            Document.document_id == document_id,
            Document.team_id == team_id,
        )
        if conversation_id is not None:
            stmt = stmt.where(Document.conversation_id == conversation_id)
        document = self.db.scalar(stmt)
        if document is None:
            raise EntityNotFoundError(f"Document '{document_id}' not found in team '{team_id}'.")
        if user_id is not None:
            if document.conversation_id is not None:
                self._ensure_conversation_access(
                    team_id=team_id,
                    conversation_id=document.conversation_id,
                    user_id=user_id,
                )
            elif document.space_id is not None:
                self.resolve_space_id(
                    team_id=team_id,
                    conversation_id=None,
                    space_id=document.space_id,
                    user_id=user_id,
                )
        return document

    def update_document_status(
        self,
        *,
        document_id: str,
        team_id: str,
        status: str,
        conversation_id: str | None = None,
    ) -> Document:
        document = self.get_document_in_team(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
        )
        document.status = status
        document.updated_at = datetime.now(timezone.utc)
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document

    def resolve_original_file(
        self,
        *,
        document_id: str,
        team_id: str,
        conversation_id: str | None,
        user_id: str | None = None,
    ) -> tuple[Path, Document]:
        document = self.get_document_in_team(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        if document.storage_key.startswith("inline://"):
            raise EntityNotFoundError(f"Document '{document_id}' has no uploaded original file.")
        path = self.upload_root / document.storage_key
        if not path.exists() or not path.is_file():
            raise EntityNotFoundError(f"Original file for document '{document_id}' was not found.")
        return path, document

    def delete_document(
        self,
        *,
        document_id: str,
        team_id: str,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        document = self.get_document_in_team(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )

        self._best_effort_delete_file(document)
        self.db.execute(
            delete(ChunkEmbedding).where(
                ChunkEmbedding.document_id == document_id,
                ChunkEmbedding.team_id == team_id,
            )
        )
        self.db.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.team_id == team_id,
            )
        )
        self.db.execute(
            delete(Document).where(
                Document.document_id == document_id,
                Document.team_id == team_id,
            )
        )
        self.db.commit()

    def publish_document_to_library(
        self,
        *,
        team_id: str,
        user_id: str | None,
        document_id: str,
        conversation_id: str | None,
        space_id: str | None,
        source_name: str | None = None,
    ) -> Document:
        source_document = self.get_document_in_team(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        if source_document.status != "ready":
            raise DomainValidationError("Only ready documents can be published to the library.")

        target_space_id = self.resolve_space_id(
            team_id=team_id,
            conversation_id=source_document.conversation_id,
            space_id=space_id,
            user_id=user_id,
        )
        if target_space_id is None:
            raise DomainValidationError("space_id is required to publish a library document.")

        now = datetime.now(timezone.utc)
        new_document_id = str(uuid4())
        target_source_name = (source_name or source_document.source_name).strip() or source_document.source_name
        storage_key = self._clone_storage_from_document(
            source_document=source_document,
            target_document_id=new_document_id,
            source_name=target_source_name,
        )

        published_document = Document(
            document_id=new_document_id,
            team_id=team_id,
            conversation_id=None,
            space_id=target_space_id,
            source_name=target_source_name,
            content_type=source_document.content_type,
            mime_type=source_document.mime_type,
            size_bytes=source_document.size_bytes,
            sha256=source_document.sha256,
            storage_key=storage_key,
            preview_key=None,
            page_count=source_document.page_count,
            failure_stage=None,
            error_code=None,
            error_message=None,
            meta_json=source_document.meta_json,
            visibility="space",
            asset_kind="knowledge_doc",
            retrieval_enabled=True,
            origin_document_id=source_document.document_id,
            status=source_document.status,
            content=source_document.content,
            created_at=now,
            updated_at=now,
        )
        self.db.add(published_document)
        self.db.flush()

        chunk_id_map: dict[str, str] = {}
        source_chunks = list(
            self.db.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == source_document.document_id)
                .order_by(DocumentChunk.chunk_index.asc())
            ).all()
        )
        for source_chunk in source_chunks:
            new_chunk_id = str(uuid4())
            chunk_id_map[source_chunk.chunk_id] = new_chunk_id
            self.db.add(
                DocumentChunk(
                    chunk_id=new_chunk_id,
                    document_id=published_document.document_id,
                    team_id=source_chunk.team_id,
                    chunk_index=source_chunk.chunk_index,
                    content=source_chunk.content,
                    start_char=source_chunk.start_char,
                    end_char=source_chunk.end_char,
                    page_no=source_chunk.page_no,
                    locator_label=source_chunk.locator_label,
                    block_type=source_chunk.block_type,
                    meta_json=source_chunk.meta_json,
                    created_at=now,
                )
            )

        source_embeddings = list(
            self.db.scalars(
                select(ChunkEmbedding).where(ChunkEmbedding.document_id == source_document.document_id)
            ).all()
        )
        for source_embedding in source_embeddings:
            mapped_chunk_id = chunk_id_map.get(source_embedding.chunk_id)
            if mapped_chunk_id is None:
                continue
            self.db.add(
                ChunkEmbedding(
                    embedding_id=str(uuid4()),
                    chunk_id=mapped_chunk_id,
                    document_id=published_document.document_id,
                    team_id=source_embedding.team_id,
                    embedding_model=source_embedding.embedding_model,
                    vector_json=source_embedding.vector_json,
                    vector_dim=source_embedding.vector_dim,
                    created_at=now,
                    updated_at=now,
                )
            )

        self.db.commit()
        self.db.refresh(published_document)
        return published_document

    def _parse_phase(self, document: Document) -> tuple[ParseResult | None, PipelineError | None]:
        self._set_status(document, "parsing")
        try:
            parse_result = self._parse_document(document)
        except DomainValidationError as exc:
            return None, self._to_pipeline_error(stage="parse", exc=exc)
        except Exception as exc:  # pragma: no cover - defensive guard
            return None, PipelineError(stage="parse", code="PARSE_FAILED", message=str(exc))

        if not parse_result.text.strip():
            return None, PipelineError(
                stage="parse",
                code="NO_TEXT_EXTRACTED",
                message="No text could be extracted from the uploaded file.",
            )

        document.content = parse_result.text
        document.page_count = parse_result.page_count
        document.meta_json = self._merge_meta_json(document.meta_json, parse_result.meta)
        document.failure_stage = None
        document.error_code = None
        document.error_message = None
        self._set_status(document, "uploaded")
        return parse_result, None

    def _chunk_phase(
        self,
        *,
        document: Document,
        team_id: str,
        conversation_id: str | None,
        max_chars: int,
        overlap: int,
        sections: list[ChunkSection] | None,
        chunk_service: ChunkService,
    ) -> tuple[list[DocumentChunk] | None, PipelineError | None]:
        try:
            chunks = chunk_service.chunk_document(
                document_id=document.document_id,
                team_id=team_id,
                conversation_id=conversation_id,
                max_chars=max_chars,
                overlap=overlap,
                sections=sections,
            )
            return chunks, None
        except DomainValidationError as exc:
            return None, self._to_pipeline_error(stage="chunk", exc=exc)
        except Exception as exc:  # pragma: no cover - defensive guard
            return None, PipelineError(stage="chunk", code="CHUNK_FAILED", message=str(exc))

    def _index_phase(
        self,
        *,
        team_id: str,
        user_id: str | None,
        conversation_id: str | None,
        document_id: str,
        embedding_model: str | None,
        retrieval_service: RetrievalService,
    ) -> PipelineError | None:
        try:
            retrieval_service.index_chunks(
                team_id=team_id,
                user_id=user_id,
                document_ids=[document_id],
                conversation_id=conversation_id,
                embedding_model=embedding_model,
                rebuild=True,
            )
            return None
        except DomainValidationError as exc:
            return self._to_pipeline_error(stage="index", exc=exc)
        except EntityNotFoundError as exc:
            return PipelineError(stage="index", code="INDEX_FAILED", message=str(exc))
        except Exception as exc:  # pragma: no cover - defensive guard
            return PipelineError(stage="index", code="INDEX_FAILED", message=str(exc))

    def _mark_failed(self, *, document: Document, error: PipelineError) -> None:
        document.status = "failed"
        document.failure_stage = error.stage
        document.error_code = error.code
        document.error_message = error.message
        document.updated_at = datetime.now(timezone.utc)
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)

    def _clear_failure(self, document: Document) -> None:
        document.failure_stage = None
        document.error_code = None
        document.error_message = None
        document.updated_at = datetime.now(timezone.utc)
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)

    def _set_status(self, document: Document, status: str) -> None:
        document.status = status
        document.updated_at = datetime.now(timezone.utc)
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)

    def _parse_document(self, document: Document) -> ParseResult:
        if document.storage_key.startswith("inline://"):
            sections = [ChunkSection(text=document.content)]
            return ParseResult(
                text=document.content,
                sections=sections,
                page_count=None,
                meta={"inline": True},
            )

        path = self.upload_root / document.storage_key
        if not path.exists() or not path.is_file():
            raise DomainValidationError("PARSE_FAILED: original uploaded file does not exist.")
        file_bytes = path.read_bytes()

        if document.content_type in {"txt", "md"}:
            return self._parse_text_file(document=document, file_bytes=file_bytes)
        if document.content_type == "pdf":
            return self._parse_pdf_file(document=document, file_bytes=file_bytes)
        if document.content_type == "docx":
            return self._parse_docx_file(document=document, file_bytes=file_bytes)
        if document.content_type == "xlsx":
            return self._parse_xlsx_file(document=document, file_bytes=file_bytes)
        if document.content_type in {"png", "jpg", "jpeg", "webp"}:
            return self._parse_image_file(document=document, file_bytes=file_bytes)
        raise DomainValidationError("UNSUPPORTED_FILE_TYPE: unsupported document type.")

    def _parse_text_file(self, *, document: Document, file_bytes: bytes) -> ParseResult:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainValidationError("UNSUPPORTED_TEXT_ENCODING: only UTF-8 text is supported.") from exc
        normalized = text.strip()
        return ParseResult(
            text=normalized,
            sections=[ChunkSection(text=normalized)] if normalized else [],
            page_count=None,
            meta={"mime_type": document.mime_type, "ocr_used": False},
        )

    def _parse_pdf_file(self, *, document: Document, file_bytes: bytes) -> ParseResult:
        reader = PdfReader(BytesIO(file_bytes))
        page_count = len(reader.pages)
        sections: list[ChunkSection] = []
        merged_parts: list[str] = []

        for index, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue
            if merged_parts:
                merged_parts.append("\n\n")
            merged_parts.append(page_text)
            sections.append(
                ChunkSection(
                    text=page_text,
                    page_no=index,
                    locator_label=f"Page {index}",
                    block_type="paragraph",
                )
            )

        text = "".join(merged_parts)
        if not text:
            text = f"PDF attachment: {document.source_name}"
            sections = [
                ChunkSection(
                    text=text,
                    page_no=1 if page_count else None,
                    locator_label="Page 1" if page_count else None,
                    block_type="paragraph",
                )
            ]
        meta = {"mime_type": "application/pdf", "ocr_used": False}
        return ParseResult(text=text, sections=sections, page_count=page_count, meta=meta)

    def _parse_docx_file(self, *, document: Document, file_bytes: bytes) -> ParseResult:
        with self._open_zip_archive(file_bytes=file_bytes, content_type="docx") as archive:
            sections: list[ChunkSection] = []
            part_names = ["word/document.xml"]
            part_names.extend(
                sorted(
                    name
                    for name in archive.namelist()
                    if re.fullmatch(r"word/header\d+\.xml", name) or re.fullmatch(r"word/footer\d+\.xml", name)
                )
            )
            part_names.extend(name for name in ("word/footnotes.xml", "word/endnotes.xml") if name in archive.namelist())

            for part_name in part_names:
                root = self._load_xml_from_archive(archive=archive, entry_name=part_name)
                if root is None:
                    continue
                for paragraph_text in self._extract_docx_paragraphs(root):
                    sections.append(
                        ChunkSection(
                            text=paragraph_text,
                            locator_label=f"Paragraph {len(sections) + 1}",
                            block_type="paragraph",
                        )
                    )

        if not sections:
            fallback_text = f"Word attachment: {document.source_name}"
            sections = [
                ChunkSection(
                    text=fallback_text,
                    locator_label="Paragraph 1",
                    block_type="paragraph",
                )
            ]
            text = fallback_text
        else:
            text = "\n\n".join(section.text for section in sections).strip()

        return ParseResult(
            text=text,
            sections=sections,
            page_count=None,
            meta={
                "mime_type": document.mime_type,
                "paragraph_count": len(sections),
            },
        )

    def _parse_xlsx_file(self, *, document: Document, file_bytes: bytes) -> ParseResult:
        with self._open_zip_archive(file_bytes=file_bytes, content_type="xlsx") as archive:
            shared_strings = self._load_xlsx_shared_strings(archive)
            sheet_entries = self._load_xlsx_sheet_entries(archive)
            sheet_names = [sheet_name for sheet_name, _ in sheet_entries]
            sections: list[ChunkSection] = []
            merged_lines: list[str] = []
            row_count = 0

            for sheet_name, entry_name in sheet_entries:
                root = self._load_xml_from_archive(archive=archive, entry_name=entry_name)
                if root is None:
                    continue
                row_entries = self._extract_xlsx_rows(root=root, shared_strings=shared_strings)
                if not row_entries:
                    continue
                if merged_lines:
                    merged_lines.append("")
                merged_lines.append(f"Sheet: {sheet_name}")
                for row_number, row_text in row_entries:
                    row_count += 1
                    merged_lines.append(f"Row {row_number}: {row_text}")
                    sections.append(
                        ChunkSection(
                            text=f"Sheet: {sheet_name}\nRow {row_number}: {row_text}",
                            locator_label=f"Sheet {sheet_name} Row {row_number}",
                            block_type="table_row",
                        )
                    )

        if not sections:
            fallback_text = f"Excel attachment: {document.source_name}"
            sections = [
                ChunkSection(
                    text=fallback_text,
                    locator_label="Sheet 1 Row 1",
                    block_type="table_row",
                )
            ]
            text = fallback_text
        else:
            text = "\n".join(merged_lines).strip()

        return ParseResult(
            text=text,
            sections=sections,
            page_count=None,
            meta={
                "mime_type": document.mime_type,
                "sheet_count": len(sheet_names),
                "row_count": row_count,
                "sheet_names": sheet_names,
            },
        )

    def _parse_image_file(self, *, document: Document, file_bytes: bytes) -> ParseResult:
        width = None
        height = None
        text = ""
        ocr_used = False

        if Image is not None:
            with Image.open(BytesIO(file_bytes)) as img:
                width, height = img.size
                if pytesseract is not None:
                    try:
                        text = (pytesseract.image_to_string(img) or "").strip()
                        ocr_used = bool(text)
                    except Exception:
                        text = ""
                        ocr_used = False

        if not text:
            text = f"Image attachment: {document.source_name}"

        return ParseResult(
            text=text,
            sections=[
                ChunkSection(
                    text=text,
                    page_no=1,
                    locator_label="Page 1",
                    block_type="ocr",
                )
            ],
            page_count=1,
            meta={
                "mime_type": document.mime_type,
                "ocr_used": ocr_used,
                "ocr_engine": "pytesseract" if ocr_used else "none",
                "width": width,
                "height": height,
            },
        )

    def _ensure_team_and_conversation(self, *, team_id: str, conversation_id: str | None) -> None:
        team = self.db.get(Team, team_id)
        if team is None:
            raise EntityNotFoundError(f"Team '{team_id}' does not exist.")

        if conversation_id is None:
            return

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            raise EntityNotFoundError(f"Conversation '{conversation_id}' does not exist.")
        if conversation.team_id != team_id:
            raise EntityNotFoundError(
                f"Conversation '{conversation_id}' does not belong to team '{team_id}'."
            )

    def _ensure_conversation_access(
        self,
        *,
        team_id: str,
        conversation_id: str,
        user_id: str | None,
    ) -> Conversation:
        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None or conversation.team_id != team_id:
            raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
        if user_id is not None and conversation.user_id != user_id:
            raise EntityNotFoundError(f"Conversation '{conversation_id}' not found.")
        return conversation

    def _detect_content_type(self, *, source_name: str) -> str:
        normalized = source_name.strip()
        if not normalized:
            raise DomainValidationError("UNSUPPORTED_FILE_TYPE: file name is required.")
        suffix = Path(normalized).suffix.lower().lstrip(".")
        if suffix not in self._ALLOWED_TYPES:
            raise DomainValidationError(
                "UNSUPPORTED_FILE_TYPE: only txt/md/pdf/docx/xlsx/png/jpg/jpeg/webp are supported."
            )
        return suffix

    def _sniff_mime(self, *, content_type: str, file_bytes: bytes) -> str:
        if content_type in {"txt", "md"}:
            if b"\x00" in file_bytes:
                raise DomainValidationError("UNSUPPORTED_TEXT_ENCODING: binary data detected for text file.")
            try:
                file_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DomainValidationError("UNSUPPORTED_TEXT_ENCODING: only UTF-8 text is supported.") from exc
            return self._MIME_BY_TYPE[content_type]

        if content_type == "pdf":
            if not file_bytes.startswith(b"%PDF-"):
                raise DomainValidationError("UNSUPPORTED_FILE_TYPE: file bytes are not a valid PDF signature.")
            return "application/pdf"

        if content_type == "docx":
            with self._open_zip_archive(file_bytes=file_bytes, content_type=content_type) as archive:
                if "word/document.xml" not in set(archive.namelist()):
                    raise DomainValidationError("UNSUPPORTED_FILE_TYPE: file bytes are not a valid DOCX package.")
            return self._MIME_BY_TYPE[content_type]

        if content_type == "xlsx":
            with self._open_zip_archive(file_bytes=file_bytes, content_type=content_type) as archive:
                if "xl/workbook.xml" not in set(archive.namelist()):
                    raise DomainValidationError("UNSUPPORTED_FILE_TYPE: file bytes are not a valid XLSX package.")
            return self._MIME_BY_TYPE[content_type]

        if content_type == "png":
            if file_bytes[:8] != b"\x89PNG\r\n\x1a\n":
                raise DomainValidationError("UNSUPPORTED_FILE_TYPE: file bytes are not a valid PNG signature.")
            return "image/png"

        if content_type in {"jpg", "jpeg"}:
            if not file_bytes.startswith(b"\xff\xd8\xff"):
                raise DomainValidationError("UNSUPPORTED_FILE_TYPE: file bytes are not a valid JPEG signature.")
            return "image/jpeg"

        if content_type == "webp":
            if len(file_bytes) < 12 or file_bytes[:4] != b"RIFF" or file_bytes[8:12] != b"WEBP":
                raise DomainValidationError("UNSUPPORTED_FILE_TYPE: file bytes are not a valid WEBP signature.")
            return "image/webp"

        raise DomainValidationError("UNSUPPORTED_FILE_TYPE: unsupported file type.")

    def _open_zip_archive(self, *, file_bytes: bytes, content_type: str) -> zipfile.ZipFile:
        stream = BytesIO(file_bytes)
        if not zipfile.is_zipfile(stream):
            raise DomainValidationError(
                f"UNSUPPORTED_FILE_TYPE: file bytes are not a valid {content_type.upper()} archive."
            )
        stream.seek(0)
        try:
            return zipfile.ZipFile(stream)
        except zipfile.BadZipFile as exc:
            raise DomainValidationError(
                f"UNSUPPORTED_FILE_TYPE: file bytes are not a valid {content_type.upper()} archive."
            ) from exc

    def _load_xml_from_archive(
        self,
        *,
        archive: zipfile.ZipFile,
        entry_name: str,
    ) -> ET.Element | None:
        try:
            payload = archive.read(entry_name)
        except KeyError:
            return None
        try:
            return ET.fromstring(payload)
        except ET.ParseError as exc:
            raise DomainValidationError(f"PARSE_FAILED: invalid XML content in '{entry_name}'.") from exc

    def _extract_docx_paragraphs(self, root: ET.Element) -> list[str]:
        paragraph_tag = f"{{{self._WORDPROCESSING_NS}}}p"
        paragraphs: list[str] = []
        for paragraph in root.iter(paragraph_tag):
            text = self._flatten_docx_paragraph(paragraph)
            if text:
                paragraphs.append(text)
        return paragraphs

    def _flatten_docx_paragraph(self, paragraph: ET.Element) -> str:
        text_tag = f"{{{self._WORDPROCESSING_NS}}}t"
        tab_tag = f"{{{self._WORDPROCESSING_NS}}}tab"
        break_tags = {
            f"{{{self._WORDPROCESSING_NS}}}br",
            f"{{{self._WORDPROCESSING_NS}}}cr",
        }
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == text_tag:
                parts.append(node.text or "")
            elif node.tag == tab_tag:
                parts.append("\t")
            elif node.tag in break_tags:
                parts.append("\n")
        text = "".join(parts).replace("\xa0", " ").strip()
        normalized_lines = [line.strip() for line in text.splitlines() if line.strip()]
        if normalized_lines:
            return "\n".join(normalized_lines)
        return text

    def _load_xlsx_shared_strings(self, archive: zipfile.ZipFile) -> list[str]:
        root = self._load_xml_from_archive(archive=archive, entry_name="xl/sharedStrings.xml")
        if root is None:
            return []
        item_tag = f"{{{self._SPREADSHEET_NS}}}si"
        return [self._flatten_spreadsheet_text(item) for item in root.findall(item_tag)]

    def _load_xlsx_sheet_entries(self, archive: zipfile.ZipFile) -> list[tuple[str, str]]:
        workbook_root = self._load_xml_from_archive(archive=archive, entry_name="xl/workbook.xml")
        if workbook_root is None:
            raise DomainValidationError("PARSE_FAILED: workbook definition is missing.")

        rels_root = self._load_xml_from_archive(archive=archive, entry_name="xl/_rels/workbook.xml.rels")
        if rels_root is None:
            raise DomainValidationError("PARSE_FAILED: workbook relationships are missing.")

        relationship_tag = f"{{{self._PACKAGE_REL_NS}}}Relationship"
        relationships: dict[str, str] = {}
        for relation in rels_root.findall(relationship_tag):
            relation_id = relation.attrib.get("Id", "").strip()
            target = relation.attrib.get("Target", "").strip()
            if relation_id and target:
                relationships[relation_id] = target

        sheet_tag = f"{{{self._SPREADSHEET_NS}}}sheet"
        relation_attr = f"{{{self._OFFICE_DOC_REL_NS}}}id"
        sheets: list[tuple[str, str]] = []
        for index, sheet in enumerate(workbook_root.findall(f".//{sheet_tag}"), start=1):
            sheet_name = sheet.attrib.get("name", f"Sheet{index}").strip() or f"Sheet{index}"
            relation_id = sheet.attrib.get(relation_attr, "").strip()
            target = relationships.get(relation_id, "")
            if not target:
                continue
            sheets.append((sheet_name, self._resolve_office_entry_path(base_dir="xl", target=target)))
        return sheets

    def _resolve_office_entry_path(self, *, base_dir: str, target: str) -> str:
        normalized = target.replace("\\", "/").strip()
        if not normalized:
            return ""
        if normalized.startswith("/"):
            return normalized.lstrip("/")
        return posixpath.normpath(posixpath.join(base_dir, normalized))

    def _extract_xlsx_rows(
        self,
        *,
        root: ET.Element,
        shared_strings: list[str],
    ) -> list[tuple[int, str]]:
        row_tag = f"{{{self._SPREADSHEET_NS}}}row"
        cell_tag = f"{{{self._SPREADSHEET_NS}}}c"
        rows: list[tuple[int, str]] = []
        for fallback_row_number, row in enumerate(root.iter(row_tag), start=1):
            row_number_text = row.attrib.get("r", "").strip()
            try:
                row_number = int(row_number_text)
            except ValueError:
                row_number = fallback_row_number

            cell_values: list[str] = []
            for cell in row.findall(cell_tag):
                cell_ref = cell.attrib.get("r", "").strip()
                column_label = re.sub(r"\d+", "", cell_ref) or f"C{len(cell_values) + 1}"
                value = self._extract_xlsx_cell_value(cell, shared_strings)
                if value:
                    cell_values.append(f"{column_label}: {value}")

            if cell_values:
                rows.append((row_number, " | ".join(cell_values)))
        return rows

    def _extract_xlsx_cell_value(self, cell: ET.Element, shared_strings: list[str]) -> str:
        value_tag = f"{{{self._SPREADSHEET_NS}}}v"
        formula_tag = f"{{{self._SPREADSHEET_NS}}}f"
        inline_string_tag = f"{{{self._SPREADSHEET_NS}}}is"
        cell_type = cell.attrib.get("t", "n").strip()
        value_node = cell.find(value_tag)
        formula_node = cell.find(formula_tag)
        inline_string_node = cell.find(inline_string_tag)
        raw_value = (value_node.text or "").strip() if value_node is not None and value_node.text else ""

        if cell_type == "inlineStr":
            return self._flatten_spreadsheet_text(inline_string_node)

        if cell_type == "s":
            if not raw_value:
                return ""
            try:
                index = int(raw_value)
            except ValueError:
                return ""
            if 0 <= index < len(shared_strings):
                return shared_strings[index]
            return ""

        if cell_type == "b":
            if raw_value == "1":
                return "TRUE"
            if raw_value == "0":
                return "FALSE"
            return raw_value

        if cell_type in {"str", "e"}:
            if raw_value:
                return raw_value
            if formula_node is not None and formula_node.text:
                return formula_node.text.strip()
            return ""

        if raw_value:
            return raw_value

        if formula_node is not None and formula_node.text:
            return f"={formula_node.text.strip()}"
        return ""

    def _flatten_spreadsheet_text(self, node: ET.Element | None) -> str:
        if node is None:
            return ""
        text_tag = f"{{{self._SPREADSHEET_NS}}}t"
        return "".join(text.text or "" for text in node.iter(text_tag)).strip()

    def _validate_declared_mime(
        self,
        *,
        content_type: str,
        declared_mime_type: str | None,
        sniffed_mime_type: str,
    ) -> None:
        declared = str(declared_mime_type or "").split(";", 1)[0].strip().lower()
        if not declared or declared == "application/octet-stream":
            return
        allowed = set(self._MIME_ALIASES.get(content_type, set()))
        allowed.add(sniffed_mime_type)
        if declared not in allowed:
            raise DomainValidationError(
                f"UNSUPPORTED_FILE_TYPE: MIME mismatch (declared={declared}, detected={sniffed_mime_type})."
            )

    def _resolve_upload_root(self) -> Path:
        configured = Path(self.settings.upload_root_dir)
        if not configured.is_absolute():
            configured = PROJECT_ROOT / configured
        configured.mkdir(parents=True, exist_ok=True)
        return configured

    def _build_storage_key(self, *, team_id: str, document_id: str, source_name: str) -> str:
        safe_name = self._sanitize_file_name(source_name)
        return f"{team_id}/{document_id}/original/{safe_name}"

    def _sanitize_file_name(self, source_name: str) -> str:
        base_name = Path(source_name).name.strip()
        if not base_name:
            base_name = "uploaded"
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", base_name).strip("._")
        if not normalized:
            normalized = f"uploaded_{uuid4().hex[:8]}"
        return normalized[:255]

    def _write_file(self, *, storage_key: str, file_bytes: bytes) -> None:
        target = self.upload_root / storage_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file_bytes)

    def _clone_storage_from_document(
        self,
        *,
        source_document: Document,
        target_document_id: str,
        source_name: str,
    ) -> str:
        if source_document.storage_key.startswith("inline://"):
            return f"inline://publish/{uuid4()}"

        source_path = self.upload_root / source_document.storage_key
        if not source_path.exists() or not source_path.is_file():
            raise EntityNotFoundError(
                f"Original file for document '{source_document.document_id}' was not found."
            )

        storage_key = self._build_storage_key(
            team_id=source_document.team_id,
            document_id=target_document_id,
            source_name=source_name,
        )
        self._write_file(storage_key=storage_key, file_bytes=source_path.read_bytes())
        return storage_key

    def _to_data_url(self, *, mime_type: str, file_bytes: bytes) -> str:
        encoded = base64.b64encode(file_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _best_effort_delete_file(self, document: Document) -> None:
        if document.storage_key.startswith("inline://"):
            return
        target = self.upload_root / document.storage_key
        try:
            if target.exists() and target.is_file():
                target.unlink()
            document_root = target.parent.parent if target.parent.name == "original" else target.parent
            if document_root.exists():
                for child in sorted(document_root.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink(missing_ok=True)
                    else:
                        child.rmdir()
                document_root.rmdir()
        except OSError:
            # Best effort cleanup only.
            return

    def _merge_meta_json(
        self,
        current_meta_json: str | None,
        new_meta: dict[str, object] | None,
    ) -> str | None:
        merged: dict[str, object] = {}

        if current_meta_json:
            try:
                decoded = json.loads(current_meta_json)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                merged.update(decoded)

        if new_meta:
            merged.update(new_meta)

        if not merged:
            return None
        return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))

    def _to_pipeline_error(self, *, stage: str, exc: DomainValidationError) -> PipelineError:
        message = str(exc)
        code = self._extract_error_code(message)
        fallback_code = {"parse": "PARSE_FAILED", "chunk": "CHUNK_FAILED", "index": "INDEX_FAILED"}.get(
            stage, "PARSE_FAILED"
        )
        return PipelineError(stage=stage, code=code or fallback_code, message=message)

    def _extract_error_code(self, message: str) -> str | None:
        head = message.split(":", 1)[0].strip().upper().replace(" ", "_")
        if head in {
            "UNSUPPORTED_FILE_TYPE",
            "FILE_TOO_LARGE",
            "EMPTY_FILE",
            "UNSUPPORTED_TEXT_ENCODING",
            "PARSE_FAILED",
            "NO_TEXT_EXTRACTED",
            "CHUNK_FAILED",
            "INDEX_FAILED",
        }:
            return head
        return None
