from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.chunk_embedding import ChunkEmbedding
from app.models.document import Document
from app.models.document_chunk import DocumentChunk


@dataclass(frozen=True)
class ChunkSection:
    text: str
    page_no: int | None = None
    locator_label: str | None = None
    block_type: str | None = None
    meta_json: str | None = None


class ChunkService:
    # CJK sentence punctuation used as split boundaries for text without newlines/spaces.
    _CJK_BOUNDARY_CHARS = "。！？；，"

    def __init__(self, db: Session) -> None:
        self.db = db

    def chunk_document(
        self,
        document_id: str,
        team_id: str,
        conversation_id: str | None,
        max_chars: int,
        overlap: int,
        sections: list[ChunkSection] | None = None,
    ) -> list[DocumentChunk]:
        if overlap >= max_chars:
            raise DomainValidationError("overlap must be smaller than max_chars.")

        document = self._get_document_in_team(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
        )
        document.status = "chunking"
        self.db.add(document)
        self.db.commit()

        try:
            pieces = self._split_with_sections(
                text=document.content,
                sections=sections,
                max_chars=max_chars,
                overlap=overlap,
            )

            # Re-chunking should invalidate existing embeddings for this document.
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

            chunks: list[DocumentChunk] = []
            for index, (content, start_char, end_char, section) in enumerate(pieces):
                chunk = DocumentChunk(
                    chunk_id=str(uuid4()),
                    document_id=document_id,
                    team_id=team_id,
                    chunk_index=index,
                    content=content,
                    start_char=start_char,
                    end_char=end_char,
                    page_no=section.page_no if section is not None else None,
                    locator_label=section.locator_label if section is not None else None,
                    block_type=section.block_type if section is not None else None,
                    meta_json=section.meta_json if section is not None else None,
                )
                chunks.append(chunk)

            self.db.add_all(chunks)
            document.status = "uploaded"
            self.db.add(document)
            self.db.commit()
        except Exception:
            document.status = "failed"
            self.db.add(document)
            self.db.commit()
            raise

        return self.list_chunks(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
        )

    def list_chunks(
        self,
        document_id: str,
        team_id: str,
        conversation_id: str | None,
    ) -> list[DocumentChunk]:
        self._get_document_in_team(
            document_id=document_id,
            team_id=team_id,
            conversation_id=conversation_id,
        )

        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.team_id == team_id,
            )
            .order_by(DocumentChunk.chunk_index.asc())
        )
        return list(self.db.scalars(stmt).all())

    def _get_document_in_team(
        self,
        document_id: str,
        team_id: str,
        conversation_id: str | None,
    ) -> Document:
        stmt = select(Document).where(
            Document.document_id == document_id,
            Document.team_id == team_id,
        )
        if conversation_id is not None:
            stmt = stmt.where(Document.conversation_id == conversation_id)
        document = self.db.scalar(stmt)
        if document is None:
            raise EntityNotFoundError(
                f"Document '{document_id}' not found in team '{team_id}'."
            )

        return document

    def _split_with_sections(
        self,
        *,
        text: str,
        sections: list[ChunkSection] | None,
        max_chars: int,
        overlap: int,
    ) -> list[tuple[str, int, int, ChunkSection | None]]:
        if not sections:
            return [
                (content, start_char, end_char, None)
                for content, start_char, end_char in self._split_text(text=text, max_chars=max_chars, overlap=overlap)
            ]

        pieces: list[tuple[str, int, int, ChunkSection | None]] = []
        cursor = 0
        for section in sections:
            section_text = section.text.strip()
            if not section_text:
                continue
            if cursor > 0:
                cursor += 2
            section_start = cursor
            local_pieces = self._split_text(text=section_text, max_chars=max_chars, overlap=overlap)
            for content, start_char, end_char in local_pieces:
                pieces.append((content, section_start + start_char, section_start + end_char, section))
            cursor = section_start + len(section_text)

        if not pieces:
            raise DomainValidationError("Document content is empty.")
        return pieces

    def _split_text(self, text: str, max_chars: int, overlap: int) -> list[tuple[str, int, int]]:
        normalized = text.strip()
        if not normalized:
            raise DomainValidationError("Document content is empty.")

        pieces: list[tuple[str, int, int]] = []
        start = 0
        total_len = len(normalized)

        while start < total_len:
            end = min(start + max_chars, total_len)

            if end < total_len:
                split_pos = normalized.rfind("\n\n", start, end)
                if split_pos == -1:
                    split_pos = normalized.rfind("\n", start, end)
                if split_pos == -1:
                    # Chinese text often has no newlines/spaces; break right after
                    # the nearest CJK sentence punctuation instead of hard-cutting.
                    boundary = max(
                        normalized.rfind(char, start, end) for char in self._CJK_BOUNDARY_CHARS
                    )
                    if boundary != -1:
                        split_pos = boundary + 1
                if split_pos == -1:
                    split_pos = normalized.rfind(" ", start, end)

                if split_pos > start + (max_chars // 2):
                    end = split_pos

            chunk_text = normalized[start:end].strip()
            if chunk_text:
                pieces.append((chunk_text, start, end))

            if end >= total_len:
                break

            next_start = end - overlap
            if next_start <= start:
                next_start = end
            start = next_start

        return pieces
