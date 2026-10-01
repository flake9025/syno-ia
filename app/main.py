"""Point d'entrée ASGI de syno-ia."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import routes_admin, routes_auth, routes_chat, routes_health
from .config import get_settings
from .services import build_context

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("syno-ia")
    logger.info("Démarrage de %s %s (build %s)", settings.app_name, __version__, settings.build_sha)

    context = await build_context(settings)
    app.state.context = context

    if settings.index_on_startup:
        await context.indexer.start()
    await context.start_scheduler()

    try:
        yield
    finally:
        logger.info("Arrêt de l'application")
        await context.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="syno-ia",
        description="RAG privé pour NAS Synology, avec authentification et permissions DSM.",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.include_router(routes_health.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_admin.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'self'",
        )
        return response

    if WEB_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/favicon.svg", include_in_schema=False)
        async def favicon() -> FileResponse:
            return FileResponse(WEB_DIR / "assets" / "favicon.svg")

    @app.exception_handler(404)
    async def not_found(request: Request, exc) -> JSONResponse | FileResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Ressource introuvable"}, status_code=404)
        if WEB_DIR.is_dir():
            return FileResponse(WEB_DIR / "index.html", status_code=200)
        return JSONResponse({"detail": "Interface web absente"}, status_code=404)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - exécution locale
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
