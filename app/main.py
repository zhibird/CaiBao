from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import init_db


class UTF8JSONResponse(JSONResponse):
    """Return JSON with explicit UTF-8 charset for Windows client compatibility."""

    media_type = "application/json; charset=utf-8"


_scheduler: object = None  # ProactiveScheduler | None


def _make_proactive_tick():
    """Create a tick function for the proactive scheduler."""
    from app.db.session import SessionLocal
    from app.services.mcp_manager import MCPManager
    from app.services.proactive_gateway import ProactiveGateway
    from app.services.proactive_service import ProactiveService

    def _tick():
        db = SessionLocal()
        try:
            mcp = MCPManager()
            gateway = ProactiveGateway(mcp)
            svc = ProactiveService(db, gateway)
            svc.run_tick()
        except Exception:
            pass
        finally:
            db.close()
    return _tick


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _scheduler
    init_db()

    settings = get_settings()
    if settings.proactive_scheduler_enabled:
        from app.services.proactive_scheduler import ProactiveScheduler
        _scheduler = ProactiveScheduler(
            _make_proactive_tick(),
            interval_seconds=settings.proactive_tick_interval_seconds,
        )
        _scheduler.start()

    yield

    if _scheduler is not None:
        _scheduler.stop()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Minimal enterprise IM AI CaiBao service.",
        lifespan=lifespan,
        default_response_class=UTF8JSONResponse,
    )

    app.include_router(api_router, prefix=settings.api_prefix)

    web_dir = Path(__file__).resolve().parent / "web"
    app.mount("/web", StaticFiles(directory=web_dir), name="web")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def serve_frontend() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def serve_favicon() -> Response:
        favicon_path = web_dir / "favicon.ico"
        if favicon_path.exists():
            return FileResponse(favicon_path)
        return Response(status_code=204)

    return app


app = create_app()
