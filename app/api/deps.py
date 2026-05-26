from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings, reload_settings
from app.core.exceptions import DomainValidationError
from app.db.session import get_db_session
from app.services.agent_app_service import AgentAppService
from app.services.agent_service import AgentService
from app.services.action_chat_service import ActionChatService
from app.services.admin_service import AdminService
from app.services.auth_service import AuthService
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_service import ChatService
from app.services.chunk_service import ChunkService
from app.services.conversation_service import ConversationService
from app.services.document_service import DocumentService
from app.services.embedding_model_service import EmbeddingModelService
from app.services.embedding_service import EmbeddingService
from app.services.llm_model_service import LLMModelService
from app.services.llm_service import LLMService
from app.services.memory_service import MemoryService
from app.services.rag_chat_service import RagChatService
from app.services.retrieval_service import RetrievalService
from app.services.space_service import SpaceService
from app.services.team_service import TeamService
from app.services.tool_service import ToolService
from app.services.user_service import UserService


def get_team_service(db: Session = Depends(get_db_session)) -> TeamService:
    return TeamService(db)


def get_user_service(db: Session = Depends(get_db_session)) -> UserService:
    return UserService(db)


def get_auth_service(db: Session = Depends(get_db_session)) -> AuthService:
    return AuthService(db=db, settings=get_settings())


def require_current_user(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    try:
        return auth_service.get_current_user_from_request(request)
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def require_current_active_user(current_user=Depends(require_current_user)):
    if not current_user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive.")
    return current_user


def get_admin_service(db: Session = Depends(get_db_session)) -> AdminService:
    return AdminService(db=db, settings=get_settings())


def require_dev_admin(
    x_dev_admin_token: str | None = Header(default=None, alias="X-Dev-Admin-Token"),
    admin_service: AdminService = Depends(get_admin_service),
):
    try:
        return admin_service.authenticate(x_dev_admin_token)
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


def get_document_service(db: Session = Depends(get_db_session)) -> DocumentService:
    return DocumentService(db)


def get_space_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
) -> SpaceService:
    return SpaceService(db=db, user_service=user_service)


def get_embedding_service() -> EmbeddingService:
    return EmbeddingService(settings=reload_settings())


def get_embedding_model_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
) -> EmbeddingModelService:
    return EmbeddingModelService(db=db, user_service=user_service)


def get_memory_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
    space_service: SpaceService = Depends(get_space_service),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    embedding_model_service: EmbeddingModelService = Depends(get_embedding_model_service),
) -> MemoryService:
    return MemoryService(
        db=db,
        user_service=user_service,
        space_service=space_service,
        embedding_service=embedding_service,
        embedding_model_service=embedding_model_service,
    )


def get_conversation_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
    space_service: SpaceService = Depends(get_space_service),
) -> ConversationService:
    return ConversationService(db=db, user_service=user_service, space_service=space_service)


def get_chunk_service(db: Session = Depends(get_db_session)) -> ChunkService:
    return ChunkService(db)


def get_chat_history_service(db: Session = Depends(get_db_session)) -> ChatHistoryService:
    return ChatHistoryService(db)


def get_retrieval_service(
    db: Session = Depends(get_db_session),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    embedding_model_service: EmbeddingModelService = Depends(get_embedding_model_service),
) -> RetrievalService:
    return RetrievalService(
        db=db,
        embedding_service=embedding_service,
        embedding_model_service=embedding_model_service,
    )


def get_llm_service() -> LLMService:
    return LLMService(settings=reload_settings())


def get_llm_model_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
) -> LLMModelService:
    return LLMModelService(db=db, user_service=user_service)


def get_tool_service(db: Session = Depends(get_db_session)) -> ToolService:
    return ToolService(db)


def get_chat_service(user_service: UserService = Depends(get_user_service)) -> ChatService:
    return ChatService(user_service)


def get_rag_chat_service(
    user_service: UserService = Depends(get_user_service),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
    document_service: DocumentService = Depends(get_document_service),
    retrieval_service: RetrievalService = Depends(get_retrieval_service),
    memory_service: MemoryService = Depends(get_memory_service),
    llm_service: LLMService = Depends(get_llm_service),
    llm_model_service: LLMModelService = Depends(get_llm_model_service),
) -> RagChatService:
    return RagChatService(
        user_service=user_service,
        chat_history_service=chat_history_service,
        document_service=document_service,
        retrieval_service=retrieval_service,
        memory_service=memory_service,
        llm_service=llm_service,
        llm_model_service=llm_model_service,
    )


def get_action_chat_service(
    user_service: UserService = Depends(get_user_service),
    tool_service: ToolService = Depends(get_tool_service),
) -> ActionChatService:
    return ActionChatService(
        user_service=user_service,
        tool_service=tool_service,
    )


def get_agent_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
    document_service: DocumentService = Depends(get_document_service),
    rag_chat_service: RagChatService = Depends(get_rag_chat_service),
    tool_service: ToolService = Depends(get_tool_service),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
    llm_service: LLMService = Depends(get_llm_service),
    llm_model_service: LLMModelService = Depends(get_llm_model_service),
) -> AgentService:
    return AgentService(
        db=db,
        user_service=user_service,
        document_service=document_service,
        rag_chat_service=rag_chat_service,
        tool_service=tool_service,
        chat_history_service=chat_history_service,
        llm_service=llm_service,
        llm_model_service=llm_model_service,
    )


def get_agent_app_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
    document_service: DocumentService = Depends(get_document_service),
    agent_service: AgentService = Depends(get_agent_service),
) -> AgentAppService:
    return AgentAppService(
        db=db,
        user_service=user_service,
        document_service=document_service,
        agent_service=agent_service,
    )


def get_favorite_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
    space_service: SpaceService = Depends(get_space_service),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
    memory_service: MemoryService = Depends(get_memory_service),
) -> "FavoriteService":
    from app.services.favorite_service import FavoriteService

    return FavoriteService(
        db=db,
        user_service=user_service,
        space_service=space_service,
    )


def get_conclusion_service(
    db: Session = Depends(get_db_session),
    user_service: UserService = Depends(get_user_service),
    space_service: SpaceService = Depends(get_space_service),
    document_service: DocumentService = Depends(get_document_service),
    chunk_service: ChunkService = Depends(get_chunk_service),
    retrieval_service: RetrievalService = Depends(get_retrieval_service),
) -> object:
    from app.services.conclusion_service import ConclusionService

    return ConclusionService(
        db=db,
        user_service=user_service,
        space_service=space_service,
        document_service=document_service,
        chunk_service=chunk_service,
        retrieval_service=retrieval_service,
    )
