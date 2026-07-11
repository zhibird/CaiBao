import json
import logging
from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.conversation import Conversation
from app.models.chunk_embedding import ChunkEmbedding
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.team import Team
from app.services.embedding_model_service import EmbeddingModelService
from app.services.embedding_service import EmbeddingRuntimeConfig, EmbeddingService

_logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(
        self,
        db: Session,
        embedding_service: EmbeddingService,
        embedding_model_service: EmbeddingModelService,
    ) -> None:
        self.db = db
        self.embedding_service = embedding_service
        self.embedding_model_service = embedding_model_service

    def index_chunks(
        self,
        team_id: str,
        document_id: str | None = None,
        document_ids: list[str] | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
        embedding_model: str | None = None,
        rebuild: bool = False,
    ) -> int:
        self._ensure_team_exists(team_id=team_id)
        scoped_document_ids = self._resolve_document_ids(
            document_id=document_id,
            document_ids=document_ids,
        )
        target_documents = self._load_target_documents(
            team_id=team_id,
            conversation_id=conversation_id,
            user_id=user_id,
            document_ids=scoped_document_ids,
        )
        if not target_documents:
            raise EntityNotFoundError("No documents found for indexing scope.")
        target_document_ids = [document.document_id for document in target_documents]

        for document in target_documents:
            document.status = "indexing"
            self.db.add(document)
        self.db.commit()

        runtime_embedding = self._resolve_embedding_runtime(
            team_id=team_id,
            user_id=user_id,
            embedding_model=embedding_model,
        )

        try:
            if rebuild:
                self._delete_scope_embeddings(
                    team_id=team_id,
                    document_ids=target_document_ids,
                )

            stmt = select(DocumentChunk).where(DocumentChunk.team_id == team_id)
            if conversation_id is not None:
                if user_id is not None:
                    self._ensure_conversation_access(
                        team_id=team_id,
                        conversation_id=conversation_id,
                        user_id=user_id,
                    )
                stmt = stmt.join(Document, Document.document_id == DocumentChunk.document_id).where(
                    Document.conversation_id == conversation_id
                )
            if target_document_ids:
                stmt = stmt.where(DocumentChunk.document_id.in_(target_document_ids))

            chunks = list(self.db.scalars(stmt).all())
            if not chunks:
                raise EntityNotFoundError("No chunks found. Run document chunking first.")

            batch_size = max(1, self.embedding_service.batch_size)
            for start in range(0, len(chunks), batch_size):
                chunk_batch = chunks[start : start + batch_size]
                texts = [chunk.content for chunk in chunk_batch]
                vectors = self.embedding_service.embed_texts(texts, runtime=runtime_embedding)
                if len(vectors) != len(chunk_batch):
                    raise DomainValidationError(
                        f"Embedding count mismatch for index batch: expected {len(chunk_batch)}, got {len(vectors)}."
                    )

                for chunk, vector in zip(chunk_batch, vectors):
                    payload = json.dumps(vector, separators=(",", ":"))
                    vector_dim = len(vector)
                    if vector_dim <= 0:
                        raise DomainValidationError("Embedding vector cannot be empty.")

                    existing = self.db.scalar(
                        select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk.chunk_id)
                    )
                    if existing is None:
                        existing = ChunkEmbedding(
                            embedding_id=str(uuid4()),
                            chunk_id=chunk.chunk_id,
                            document_id=chunk.document_id,
                            team_id=chunk.team_id,
                            embedding_model=self.embedding_service.model_name,
                            vector_json=payload,
                            vector_dim=vector_dim,
                        )
                        self.db.add(existing)
                    else:
                        existing.document_id = chunk.document_id
                        existing.team_id = chunk.team_id
                        existing.embedding_model = self.embedding_service.model_name
                        existing.vector_json = payload
                        existing.vector_dim = vector_dim

            for document in target_documents:
                document.status = "ready"
                self.db.add(document)
            self.db.commit()
            return len(chunks)
        except Exception:
            for document in target_documents:
                document.status = "failed"
                self.db.add(document)
            self.db.commit()
            raise

    def search_chunks(
        self,
        team_id: str,
        query: str,
        top_k: int,
        document_id: str | None = None,
        document_ids: list[str] | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
        embedding_model: str | None = None,
    ) -> list[dict[str, object]]:
        self._ensure_team_exists(team_id=team_id)
        scoped_document_ids = self._resolve_document_ids(
            document_id=document_id,
            document_ids=document_ids,
        )
        runtime_embedding = self._resolve_embedding_runtime(
            team_id=team_id,
            user_id=user_id,
            embedding_model=embedding_model,
        )

        query_vec = self.embedding_service.embed_text(query, runtime=runtime_embedding)

        stmt = (
            select(ChunkEmbedding, DocumentChunk, Document)
            .join(DocumentChunk, ChunkEmbedding.chunk_id == DocumentChunk.chunk_id)
            .join(Document, Document.document_id == ChunkEmbedding.document_id)
            .where(ChunkEmbedding.team_id == team_id)
            # Only compare vectors produced by the same embedding model as the query;
            # mixing models (even with equal dimensions) yields meaningless scores.
            .where(ChunkEmbedding.embedding_model == self.embedding_service.model_name)
        )
        if user_id is not None:
            stmt = stmt.outerjoin(Conversation, Conversation.conversation_id == Document.conversation_id).where(
                or_(Document.conversation_id.is_(None), Conversation.user_id == user_id)
            )
        if conversation_id is not None:
            if user_id is not None:
                self._ensure_conversation_access(
                    team_id=team_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                )
            stmt = stmt.where(Document.conversation_id == conversation_id)
        if scoped_document_ids is not None:
            stmt = stmt.where(ChunkEmbedding.document_id.in_(scoped_document_ids))

        rows = self.db.execute(stmt).all()
        if not rows:
            raise EntityNotFoundError("No indexed chunks found. Run retrieval indexing first.")

        scored_hits: list[dict[str, object]] = []
        skipped_dim_mismatch = 0
        query_dim = len(query_vec)
        for embedding, chunk, document in rows:
            vector = json.loads(embedding.vector_json)
            if not isinstance(vector, list):
                raise DomainValidationError(
                    "Indexed vector payload is invalid. Rebuild retrieval index with current embedding model."
                )
            if len(vector) != query_dim:
                # Skip stale vectors instead of failing the whole search.
                skipped_dim_mismatch += 1
                continue
            score = self.embedding_service.cosine_similarity(query_vec, vector)
            scored_hits.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "source_name": document.source_name,
                    "team_id": chunk.team_id,
                    "chunk_index": chunk.chunk_index,
                    "page_no": chunk.page_no,
                    "locator_label": chunk.locator_label,
                    "content": chunk.content,
                    "score": round(score, 6),
                }
            )

        if skipped_dim_mismatch:
            _logger.warning(
                "Skipped %d indexed vector(s) with mismatched dimensions (query_dim=%d, model=%s); "
                "rebuild retrieval index to include them.",
                skipped_dim_mismatch,
                query_dim,
                self.embedding_service.model_name,
            )
        if not scored_hits:
            raise EntityNotFoundError(
                "No compatible indexed chunks found. Rebuild retrieval index with current embedding model."
            )

        scored_hits.sort(key=lambda item: item["score"], reverse=True)
        return scored_hits[:top_k]

    def has_indexed_chunks(
        self,
        team_id: str,
        document_id: str | None = None,
        document_ids: list[str] | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        self._ensure_team_exists(team_id=team_id)
        scoped_document_ids = self._resolve_document_ids(
            document_id=document_id,
            document_ids=document_ids,
        )

        stmt = (
            select(ChunkEmbedding.embedding_id)
            .join(Document, Document.document_id == ChunkEmbedding.document_id)
            .where(ChunkEmbedding.team_id == team_id)
        )
        if user_id is not None:
            stmt = stmt.outerjoin(Conversation, Conversation.conversation_id == Document.conversation_id).where(
                or_(Document.conversation_id.is_(None), Conversation.user_id == user_id)
            )
        if conversation_id is not None:
            if user_id is not None:
                self._ensure_conversation_access(
                    team_id=team_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                )
            stmt = stmt.where(Document.conversation_id == conversation_id)
        if scoped_document_ids is not None:
            stmt = stmt.where(ChunkEmbedding.document_id.in_(scoped_document_ids))

        first_embedding_id = self.db.scalar(stmt.limit(1))
        return first_embedding_id is not None

    def _resolve_embedding_runtime(
        self,
        *,
        team_id: str,
        user_id: str | None,
        embedding_model: str | None,
    ) -> EmbeddingRuntimeConfig | None:
        normalized = (embedding_model or "").strip()
        if not normalized or normalized.lower() == "default":
            return None

        if normalized.lower() in {"mock", "none"}:
            return EmbeddingRuntimeConfig.mock_default()

        if not user_id:
            raise DomainValidationError("user_id is required when embedding_model is specified.")

        return self.embedding_model_service.resolve_runtime_config(
            team_id=team_id,
            user_id=user_id,
            model_name=normalized,
        )

    def _ensure_team_exists(self, team_id: str) -> None:
        team = self.db.get(Team, team_id)
        if team is None:
            raise EntityNotFoundError(f"Team '{team_id}' does not exist.")

    def _delete_scope_embeddings(
        self,
        *,
        team_id: str,
        document_ids: list[str] | None,
    ) -> None:
        if document_ids is not None:
            self.db.execute(
                delete(ChunkEmbedding).where(
                    ChunkEmbedding.team_id == team_id,
                    ChunkEmbedding.document_id.in_(document_ids),
                )
            )
            return

        self.db.execute(delete(ChunkEmbedding).where(ChunkEmbedding.team_id == team_id))

    def _resolve_document_ids(
        self,
        *,
        document_id: str | None,
        document_ids: list[str] | None,
    ) -> list[str] | None:
        values: list[str] = []
        if document_id:
            values.append(document_id)
        if document_ids:
            values.extend(document_ids)

        deduped: list[str] = []
        for item in values:
            normalized = str(item).strip()
            if normalized and normalized not in deduped:
                deduped.append(normalized)
        return deduped or None

    def _load_target_documents(
        self,
        *,
        team_id: str,
        conversation_id: str | None,
        user_id: str | None,
        document_ids: list[str] | None,
    ) -> list[Document]:
        if conversation_id is not None and user_id is not None:
            self._ensure_conversation_access(
                team_id=team_id,
                conversation_id=conversation_id,
                user_id=user_id,
            )

        stmt = select(Document).where(Document.team_id == team_id)
        if user_id is not None:
            stmt = stmt.outerjoin(Conversation, Conversation.conversation_id == Document.conversation_id).where(
                or_(Document.conversation_id.is_(None), Conversation.user_id == user_id)
            )
        if conversation_id is not None:
            stmt = stmt.where(Document.conversation_id == conversation_id)
        if document_ids is not None:
            stmt = stmt.where(Document.document_id.in_(document_ids))
        return list(self.db.scalars(stmt).all())

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
