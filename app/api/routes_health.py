"""Sonde de santé, utilisée par Docker, le NAS et l'intégration continue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..services import AppContext

router = APIRouter(tags=["health"])


def _context(request: Request) -> AppContext | None:
    return getattr(request.app.state, "context", None)


@router.get("/api/health")
async def health(context: AppContext | None = Depends(_context)) -> dict:
    """Réponse volontairement minimale et toujours disponible (aucune authentification)."""
    if context is None:
        return {"status": "starting"}
    # Volontairement sans statistiques d'index : cette route est publique et le nombre de
    # documents indexés révélerait le contenu de partages que l'appelant n'a pas le droit de voir.
    return {
        "status": "ok",
        "version": context.settings.app_version,
        "build": context.settings.build_sha,
        "uptime": round(context.uptime, 1),
        "profile": context.hardware.profile,
        "indexing": context.indexer.progress.status,
        "llm": context.llm.name if context.llm else "extractive",
        "embeddings": context.embedder.backend,
        "embeddings_state": getattr(context.embedder, "state", "ready"),
    }


@router.get("/api/info")
async def info(context: AppContext | None = Depends(_context)) -> dict:
    """Informations publiques affichées sur la page de connexion."""
    if context is None:
        return {"app": "syno-ia", "status": "starting"}
    return {
        "app": context.settings.app_name,
        "version": context.settings.app_version,
        "language": context.settings.default_language,
        "dsm_url": context.settings.dsm_url,
        "profile": context.hardware.profile,
    }
