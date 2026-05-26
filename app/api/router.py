from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.agent import router as agent_router
from app.api.routes.apps import router as apps_router
from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.conclusion import router as conclusion_router
from app.api.routes.conversation import router as conversation_router
from app.api.routes.document import router as document_router
from app.api.routes.embedding_model import router as embedding_model_router
from app.api.routes.favorite import router as favorite_router
from app.api.routes.health import router as health_router
from app.api.routes.library import router as library_router
from app.api.routes.llm_model import router as llm_model_router
from app.api.routes.memory import router as memory_router
from app.api.routes.retrieval import router as retrieval_router
from app.api.routes.space import router as space_router
from app.api.routes.team import router as team_router
from app.api.routes.user import router as user_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router, tags=["auth"])
api_router.include_router(admin_router, tags=["admin"])
api_router.include_router(team_router, tags=["teams"])
api_router.include_router(user_router, tags=["users"])
api_router.include_router(space_router, tags=["spaces"])
api_router.include_router(conversation_router, tags=["conversations"])
api_router.include_router(library_router, tags=["library"])
api_router.include_router(memory_router, tags=["memory"])
api_router.include_router(llm_model_router, tags=["llm-models"])
api_router.include_router(embedding_model_router, tags=["embedding-models"])
api_router.include_router(document_router, tags=["documents"])
api_router.include_router(retrieval_router, tags=["retrieval"])
api_router.include_router(chat_router, tags=["chat"])
api_router.include_router(agent_router, tags=["agent"])
api_router.include_router(apps_router, tags=["apps"])
api_router.include_router(favorite_router, tags=["favorites"])
api_router.include_router(conclusion_router, tags=["conclusions"])
