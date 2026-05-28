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
from app.services.mcp_manager import MCPManager
from app.services.tool_catalog_service import ToolCatalogService
from app.services.tool_safety import ToolSafetyService
from app.services.tool_service import ToolService
from app.services.user_service import UserService
from app.services.memory_markdown_store import MemoryMarkdownStore
from app.events.event_bus import EventBus
from app.db.session import SessionLocal  # used by consolidation factory

_event_bus_singleton: EventBus | None = None
_event_bus_lock = None  # lazy-init via existing _threading import


def get_event_bus() -> EventBus:
    global _event_bus_singleton, _event_bus_lock
    if _event_bus_lock is None:
        import threading as _t
        _event_bus_lock = _t.Lock()
    if _event_bus_singleton is None:
        with _event_bus_lock:
            if _event_bus_singleton is None:
                _event_bus_singleton = EventBus()
                _event_bus_singleton.start_worker()
    return _event_bus_singleton


_plugin_mgr_singleton = None


def get_plugin_manager() -> "PluginManager | None":
    global _plugin_mgr_singleton
    from app.core.config import get_settings
    if not get_settings().plugin_enabled:
        return None
    if _plugin_mgr_singleton is None:
        from app.plugins.manager import PluginManager
        _plugin_mgr_singleton = PluginManager(event_bus=get_event_bus())
        _plugin_mgr_singleton.load_all()
    return _plugin_mgr_singleton


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


_catalog_singleton: ToolCatalogService | None = None
_safety_singleton: ToolSafetyService | None = None
_mcp_manager_singleton: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _mcp_manager_singleton
    if _mcp_manager_singleton is None:
        _mcp_manager_singleton = MCPManager()
    return _mcp_manager_singleton


def get_tool_catalog_service() -> ToolCatalogService:
    global _catalog_singleton
    if _catalog_singleton is None:
        _catalog_singleton = ToolCatalogService()
        _register_generic_tools(_catalog_singleton)
    return _catalog_singleton


def _register_generic_tools(catalog: ToolCatalogService) -> None:
    from app.services.tools.web_tools import create_web_tools
    from app.services.tools.file_tools import create_file_tools
    from app.services.tools.shell_tools import create_shell_tools

    catalog.register_generic(create_web_tools())
    catalog.register_generic(create_file_tools())
    catalog.register_generic(create_shell_tools())

    from app.core.config import get_settings as _gs2
    if _gs2().subagent_enabled:
        catalog.register_generic(_create_subagent_tool())

def _create_subagent_tool():
    from app.services.tool_registry import ToolDefinition
    return [ToolDefinition(
        name="spawn_subagent",
        display_name="生成子智能体",
        description="Spawn a sub-agent with a restricted tool profile (research or ops) to handle a sub-task.",
        dangerous=False,
        handler_key="generic.spawn_subagent",
        permission_scope="team",
        source="generic",
        provider="subagent",
        input_schema={
            "type": "object",
            "required": ["profile", "task"],
            "properties": {
                "profile": {"type": "string", "enum": ["research", "ops"]},
                "task": {"type": "string", "minLength": 1, "maxLength": 4000},
                "async_mode": {"type": "boolean", "default": False},
            },
        },
        output_schema={"type": "object"},
    )]


def get_tool_safety_service() -> ToolSafetyService:
    global _safety_singleton
    if _safety_singleton is None:
        _safety_singleton = ToolSafetyService()
    return _safety_singleton


def get_tool_service(
    db: Session = Depends(get_db_session),
    catalog: ToolCatalogService = Depends(get_tool_catalog_service),
    safety: ToolSafetyService = Depends(get_tool_safety_service),
    mcp_manager: MCPManager = Depends(get_mcp_manager),
) -> ToolService:
    svc = ToolService(db=db, catalog=catalog, safety=safety, plugin_manager=get_plugin_manager())
    _register_generic_handlers(svc)
    _register_mcp_handlers(svc, catalog, mcp_manager)
    return svc


def _register_generic_handlers(svc: ToolService) -> None:
    from app.services.tools.web_tools import web_fetch_handler, web_search_handler
    from app.services.tools.file_tools import (
        edit_file_handler,
        list_dir_handler,
        read_file_handler,
        write_file_handler,
    )

    svc.register_generic_handler("web_fetch", web_fetch_handler)
    svc.register_generic_handler("web_search", web_search_handler)
    svc.register_generic_handler("list_dir", list_dir_handler)
    svc.register_generic_handler("read_file", read_file_handler)
    svc.register_generic_handler("write_file", write_file_handler)
    svc.register_generic_handler("edit_file", edit_file_handler)

    from app.services.tools.shell_tools import (
        shell_exec_handler,
        shell_kill_handler,
        shell_status_handler,
    )

    svc.register_generic_handler("shell_exec", shell_exec_handler)
    svc.register_generic_handler("shell_status", shell_status_handler)
    svc.register_generic_handler("shell_kill", shell_kill_handler)

    # Sub-agent spawn tool
    from app.core.config import get_settings as _gs
    if _gs().subagent_enabled:
        def _spawn_subagent_handler(*, team_id, user_id, arguments):
            from app.db.session import SessionLocal
            from app.services.llm_service import LLMService
            from app.services.subagent_service import SubAgentService
            db2 = SessionLocal()
            try:
                profile = str(arguments.get("profile", "research"))
                task = str(arguments.get("task", ""))
                async_mode = bool(arguments.get("async_mode", False))
                svc_sub = SubAgentService(
                    db=db2,
                    llm_service=LLMService(settings=_gs()),
                    tool_service=svc,  # reuse the per-request ToolService
                )
                if async_mode:
                    job_id = svc_sub.run_async(profile=profile, task=task, team_id=team_id, user_id=user_id)
                    return {"job_id": job_id, "async": True}
                else:
                    result = svc_sub.run_sync(profile=profile, task=task, team_id=team_id, user_id=user_id)
                    return result
            finally:
                db2.close()
        svc.register_generic_handler("spawn_subagent", _spawn_subagent_handler)


import threading as _threading

_mcp_connect_lock = _threading.Lock()
_mcp_connected = False
_mcp_defs_cache: list | None = None


def _reset_mcp_connection() -> None:
    global _mcp_connected, _mcp_defs_cache
    with _mcp_connect_lock:
        _mcp_connected = False
        _mcp_defs_cache = None


def _finalize_mcp_reload(
    mcp_manager: MCPManager,
    catalog: ToolCatalogService,
) -> None:
    """Mark MCP as connected and populate the handler cache after an explicit reload.

    Must be called *after* the caller has already connected to all servers
    and refreshed the catalog.  This prevents the next per-request
    ``_register_mcp_handlers`` from re-entering ``connect_all()`` (which
    would ``shutdown_all()`` first).
    """
    global _mcp_connected, _mcp_defs_cache
    mcp_defs = mcp_manager.discover_tools()
    catalog.refresh_mcp(mcp_defs)

    cached_pairs: list = []
    for d in mcp_defs:
        def _make_mcp_handler(namespaced_name=d.name):
            def handler(*, team_id, user_id, arguments):
                return mcp_manager.call_tool(namespaced_name, arguments)
            return handler
        handler = _make_mcp_handler()
        cached_pairs.append((d, handler))

    with _mcp_connect_lock:
        _mcp_defs_cache = cached_pairs
        _mcp_connected = True


def _register_mcp_handlers(
    svc: ToolService,
    catalog: ToolCatalogService,
    mcp_manager: MCPManager,
) -> None:
    """Load MCP config, connect servers once, discover tools, and register handlers.

    Caches MCP tool definitions after first discovery so that subsequent
    per-request dependency injections are cheap (no re-discover / re-register).
    """
    global _mcp_connected, _mcp_defs_cache
    if not get_settings().mcp_enabled:
        return

    with _mcp_connect_lock:
        if not _mcp_connected:
            try:
                configs = mcp_manager.load_config()
            except DomainValidationError:
                configs = []
            if configs:
                mcp_manager.connect_all(configs)
            _mcp_connected = True

    # Serve from cache after initial discovery
    if _mcp_defs_cache is not None:
        for d, handler in _mcp_defs_cache:
            svc.register_generic_handler(d.name, handler)
        return

    mcp_defs = mcp_manager.discover_tools()
    catalog.refresh_mcp(mcp_defs)

    cached_pairs: list = []
    for d in mcp_defs:
        def _make_mcp_handler(namespaced_name=d.name):
            def handler(*, team_id, user_id, arguments):
                return mcp_manager.call_tool(namespaced_name, arguments)
            return handler
        handler = _make_mcp_handler()
        svc.register_generic_handler(d.name, handler)
        cached_pairs.append((d, handler))

    _mcp_defs_cache = cached_pairs


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
    from app.services.retrieval_enhancer import EnhancedRetrievalService
    enhanced = EnhancedRetrievalService(retrieval_service, llm_service)
    return RagChatService(
        user_service=user_service,
        chat_history_service=chat_history_service,
        document_service=document_service,
        retrieval_service=retrieval_service,
        memory_service=memory_service,
        llm_service=llm_service,
        llm_model_service=llm_model_service,
        enhanced_retrieval=enhanced,
    )


def get_action_chat_service(
    user_service: UserService = Depends(get_user_service),
    tool_service: ToolService = Depends(get_tool_service),
) -> ActionChatService:
    return ActionChatService(
        user_service=user_service,
        tool_service=tool_service,
    )


_memory_store_singleton: MemoryMarkdownStore | None = None
_consolidation_svc_singleton = None  # MemoryConsolidationService | None


def _get_memory_store() -> MemoryMarkdownStore:
    global _memory_store_singleton
    if _memory_store_singleton is None:
        _memory_store_singleton = MemoryMarkdownStore()
    return _memory_store_singleton


def _get_consolidation_service(llm_service=None):
    global _consolidation_svc_singleton
    if _consolidation_svc_singleton is None or llm_service is not None:
        from app.services.memory_consolidation_service import MemoryConsolidationService
        from app.core.config import get_settings as _gs
        model = _gs().llm_model or ""
        base = _gs().llm_base_url or ""
        key = _gs().llm_api_key or ""
        if not base or not key:
            model = base = key = ""  # let LLMService use provider defaults
        if _consolidation_svc_singleton is None:
            _consolidation_svc_singleton = MemoryConsolidationService(
                event_bus=get_event_bus(),
                store=_get_memory_store(),
                llm_service=llm_service,
                fast_model_name=model,
                fast_base_url=base,
                fast_api_key=key,
                memory_service_factory=_create_memory_service_for_consolidation,
            )
        elif llm_service is not None:
            _consolidation_svc_singleton._llm = llm_service
    return _consolidation_svc_singleton


def _create_memory_service_for_consolidation():
    """Create a fresh MemoryService with its own DB session for background work."""
    from app.db.session import SessionLocal
    from app.services.embedding_model_service import EmbeddingModelService
    from app.services.embedding_service import EmbeddingService
    from app.services.space_service import SpaceService
    from app.services.user_service import UserService

    db = SessionLocal()
    user_svc = UserService(db)
    space_svc = SpaceService(db=db, user_service=user_svc)
    embedding_svc = EmbeddingService(settings=reload_settings())
    emb_model_svc = EmbeddingModelService(db=db, user_service=user_svc)
    return MemoryService(
        db=db,
        user_service=user_svc,
        space_service=space_svc,
        embedding_service=embedding_svc,
        embedding_model_service=emb_model_svc,
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
    _get_consolidation_service(llm_service=llm_service)
    return AgentService(
        db=db,
        user_service=user_service,
        document_service=document_service,
        rag_chat_service=rag_chat_service,
        tool_service=tool_service,
        chat_history_service=chat_history_service,
        llm_service=llm_service,
        llm_model_service=llm_model_service,
        event_bus=get_event_bus(),
        memory_store=_get_memory_store(),
        plugin_manager=get_plugin_manager(),
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
