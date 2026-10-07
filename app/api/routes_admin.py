"""Administration : indexation, modèles, diagnostic. Réservé aux administrateurs DSM."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import overrides
from ..hardware import LLM_BY_PROFILE
from ..llm.factory import build_llm, download_model, local_model_path
from ..llm.ollama import OllamaBackend
from ..security import WebSession
from ..services import AppContext
from .deps import get_context, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@dataclass
class DownloadState:
    status: str = "idle"  # idle | running | done | error
    model: str = ""
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    path: str = ""
    errors: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "model": self.model,
            "message": self.message,
            "path": self.path,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1)
            if self.started_at
            else 0.0,
        }


_download = DownloadState()


class IndexRequest(BaseModel):
    full: bool = Field(default=False, description="Vider l'index avant de réindexer")


class ModelRequest(BaseModel):
    profile: str | None = Field(default=None, description="micro | small | medium | large")


class RemoteLLMRequest(BaseModel):
    """Configuration d'un moteur de génération déporté (Ollama)."""

    ollama_url: str = Field(default="", max_length=300)
    llm_model: str = Field(default="", max_length=120)
    llm_backend: str | None = Field(default=None, description="auto | ollama | llamacpp | none")
    llm_timeout_seconds: int | None = Field(default=None, ge=0, le=3600)


class RemoteTestRequest(BaseModel):
    ollama_url: str = Field(default="", max_length=300)


@router.get("/status")
async def status(context: AppContext = Depends(get_context)) -> dict:
    return context.status()


@router.get("/config")
async def config(context: AppContext = Depends(get_context)) -> dict:
    """Configuration effective, expurgée de tout secret."""
    settings = context.settings
    return {
        "dsm_url": settings.dsm_url,
        "dsm_service_account": settings.dsm_service_account or None,
        "index_mode": settings.index_mode,
        "index_roots": settings.index_roots,
        "index_exclude_globs": settings.index_exclude_globs,
        "index_interval_minutes": settings.index_interval_minutes,
        "index_max_file_mb": settings.index_max_file_mb,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "retrieval_top_k": context.pipeline.top_k,
        "retrieval_candidates": context.pipeline.candidates,
        "acl_strict": settings.acl_strict,
        "acl_cache_ttl": settings.acl_cache_ttl,
        "embedding_backend": context.embedder.backend,
        "embedding_model": context.embedder.model_name,
        "llm_backend": context.llm.name if context.llm else "extractive",
        "llm_model": context.llm.model if context.llm else None,
        "hardware_profile": settings.hardware_profile,
        "admin_accounts": settings.admin_accounts,
    }


# --------------------------------------------------------------------- index
@router.post("/index/start")
async def start_index(
    payload: IndexRequest, context: AppContext = Depends(get_context)
) -> dict:
    await context.refresh_service_session(force=True)
    started = await context.indexer.start(full=payload.full)
    if not started:
        raise HTTPException(status_code=409, detail="Une indexation est déjà en cours.")
    return {"started": True, "full": payload.full}


@router.post("/index/cancel")
async def cancel_index(context: AppContext = Depends(get_context)) -> dict:
    await context.indexer.cancel()
    return {"cancelled": True}


@router.get("/index/progress")
async def index_progress(context: AppContext = Depends(get_context)) -> dict:
    return context.indexer.progress.snapshot()


# ---------------------------------------------------------------- embeddings
@router.post("/embeddings/reload")
async def reload_embeddings(context: AppContext = Depends(get_context)) -> dict:
    """Relance le chargement du moteur sémantique après un échec réseau.

    Évite d'avoir à redémarrer le conteneur lorsque le téléchargement du modèle
    a échoué au démarrage : l'application tournait alors en BM25 seul.
    """
    if not context.start_embeddings():
        raise HTTPException(
            status_code=409,
            detail="Chargement déjà en cours, ou moteur fixé par la configuration.",
        )
    return {"started": True}


@router.post("/index/clear")
async def clear_index(context: AppContext = Depends(get_context)) -> dict:
    if context.indexer.running:
        raise HTTPException(status_code=409, detail="Indexation en cours, annulez-la d'abord.")
    await asyncio.to_thread(context.store.clear)
    return {"cleared": True}


@router.post("/index/optimize")
async def optimize_index(context: AppContext = Depends(get_context)) -> dict:
    if context.indexer.running:
        raise HTTPException(status_code=409, detail="Indexation en cours, annulez-la d'abord.")
    await asyncio.to_thread(context.store.optimize)
    return {"optimized": True}


@router.get("/documents")
async def all_documents(
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: AppContext = Depends(get_context),
) -> dict:
    items = await asyncio.to_thread(
        context.store.list_documents, limit=limit, offset=offset, query=q, shares=None
    )
    return {"documents": items, "count": len(items)}


# -------------------------------------------------------------------- modèles
@router.get("/models")
async def models(context: AppContext = Depends(get_context)) -> dict:
    expected = local_model_path(context.settings, context.hardware)
    catalogue = []
    for profile, choice in LLM_BY_PROFILE.items():
        path = context.settings.models_dir / choice.filename
        catalogue.append(
            {
                "profile": profile,
                "label": choice.label,
                "repo_id": choice.repo_id,
                "filename": choice.filename,
                "approx_ram_mb": choice.approx_ram_mb,
                "installed": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
                "recommended": profile == context.hardware.profile,
            }
        )
    return {
        "models": catalogue,
        "expected_path": str(expected) if expected else None,
        "download": _download.snapshot(),
        "hardware": context.hardware.to_dict(),
    }


@router.post("/models/download")
async def start_download(
    payload: ModelRequest, context: AppContext = Depends(get_context)
) -> dict:
    if _download.status == "running":
        raise HTTPException(status_code=409, detail="Un téléchargement est déjà en cours.")
    profile = payload.profile or context.hardware.profile
    choice = LLM_BY_PROFILE.get(profile)
    if choice is None:
        raise HTTPException(status_code=400, detail=f"Profil inconnu : {profile}")

    _download.status = "running"
    _download.model = choice.label
    _download.message = "Téléchargement en cours…"
    _download.started_at = time.time()
    _download.finished_at = 0.0
    _download.path = ""

    async def worker() -> None:
        try:
            path = await asyncio.to_thread(
                download_model, choice.repo_id, choice.filename, context.settings.models_dir
            )
            _download.path = str(path)
            _download.status = "done"
            _download.message = "Modèle téléchargé. Rechargement du moteur…"
            context.llm = await build_llm(context.settings, context.hardware)
            _download.message = "Modèle prêt."
        except Exception as exc:
            _download.status = "error"
            _download.message = str(exc)
            logger.exception("Téléchargement du modèle échoué : %s", exc)
        finally:
            _download.finished_at = time.time()

    asyncio.create_task(worker())
    return {"started": True, "model": choice.label}


@router.get("/models/download")
async def download_status() -> dict:
    return _download.snapshot()


@router.delete("/models")
async def delete_model(
    profile: str = Query(..., min_length=1, description="micro | small | medium | large"),
    context: AppContext = Depends(get_context),
) -> dict:
    """Supprime un modèle téléchargé pour libérer de l'espace disque.

    Si le modèle supprimé était celui en service, le moteur est reconstruit : il se
    rabat alors sur un autre modèle installé, ou sur le mode extractif.
    """
    choice = LLM_BY_PROFILE.get(profile)
    if choice is None:
        raise HTTPException(status_code=400, detail=f"Profil inconnu : {profile}")
    if _download.status == "running":
        raise HTTPException(status_code=409, detail="Un téléchargement est en cours.")

    path = context.settings.models_dir / choice.filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Ce modèle n'est pas installé.")

    in_use = str(getattr(context.llm, "model_path", "")) == str(path)
    if in_use and context.llm is not None:
        # Libère le descripteur avant de supprimer : sur certains systèmes de
        # fichiers, un modèle encore ouvert ne peut pas être effacé.
        await context.llm.aclose()
        context.llm = None

    try:
        freed = path.stat().st_size
        path.unlink()
    except OSError as exc:
        logger.exception("Suppression du modèle impossible : %s", exc)
        raise HTTPException(status_code=500, detail=f"Suppression impossible : {exc}") from exc

    # Métadonnées laissées par huggingface_hub à côté du fichier.
    residus = context.settings.models_dir / ".cache" / "huggingface" / "download"
    if residus.is_dir():
        for reste in residus.glob(f"{choice.filename}*"):
            reste.unlink(missing_ok=True)

    logger.info("Modèle supprimé : %s (%.0f Mo libérés)", choice.label, freed / 1_048_576)
    if in_use:
        context.llm = await build_llm(context.settings, context.hardware)
    return {
        "deleted": choice.label,
        "freed_bytes": freed,
        "llm": context.llm.describe() if context.llm else {"backend": "extractive"},
    }


@router.post("/llm/reload")
async def reload_llm(context: AppContext = Depends(get_context)) -> dict:
    if context.llm is not None:
        await context.llm.aclose()
    context.llm = await build_llm(context.settings, context.hardware)
    return {"llm": context.llm.describe() if context.llm else {"backend": "extractive"}}


# --------------------------------------------------------- génération déportée
def _adresse_client(request: Request) -> str:
    """Adresse du poste qui consulte l'administration.

    Elle sert à pré-remplir l'URL d'Ollama : c'est presque toujours la machine sur
    laquelle l'utilisateur travaille, et la deviner lui évite de la chercher.
    """
    transmis = request.headers.get("x-forwarded-for", "")
    if transmis:
        return transmis.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("/llm/remote")
async def remote_llm(
    request: Request, context: AppContext = Depends(get_context)
) -> dict:
    client = _adresse_client(request)
    prive = client and not client.startswith(("127.", "::1"))
    return {
        "backend": context.settings.llm_backend,
        "ollama_url": context.settings.ollama_url,
        "llm_model": context.settings.llm_model,
        "llm_timeout_seconds": context.settings.llm_timeout_seconds,
        "client_ip": client,
        "suggested_url": f"http://{client}:11434" if prive else "",
    }


@router.post("/llm/remote/test")
async def test_remote_llm(payload: RemoteTestRequest) -> dict:
    """Teste la connexion **depuis le NAS**, seul point de vue qui compte ici.

    Un Ollama joignable depuis le navigateur ne l'est pas forcément depuis le
    conteneur : c'est précisément le piège que ce test révèle.
    """
    url = (payload.ollama_url or "").strip().rstrip("/")
    try:
        overrides.valider({"ollama_url": url})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not url:
        raise HTTPException(status_code=400, detail="Indiquez une adresse à tester.")

    sonde = OllamaBackend(url, "")
    try:
        if not await sonde.health():
            return {
                "reachable": False,
                "models": [],
                "detail": (
                    "Aucune réponse. Vérifiez qu'Ollama tourne, qu'il écoute sur "
                    "toutes les interfaces (OLLAMA_HOST=0.0.0.0) et que le pare-feu "
                    "autorise le port 11434 depuis le NAS."
                ),
            }
        modeles = await sonde.list_models()
    finally:
        await sonde.aclose()

    return {
        "reachable": True,
        "models": modeles,
        "detail": (
            "Connexion établie, mais aucun modèle n'est installé : lancez "
            "« ollama pull qwen2.5:7b-instruct »."
            if not modeles
            else f"Connexion établie : {len(modeles)} modèle(s) disponible(s)."
        ),
    }


@router.post("/llm/remote")
async def save_remote_llm(
    payload: RemoteLLMRequest, context: AppContext = Depends(get_context)
) -> dict:
    valeurs: dict = {"ollama_url": payload.ollama_url, "llm_model": payload.llm_model}
    if payload.llm_backend is not None:
        valeurs["llm_backend"] = payload.llm_backend
    if payload.llm_timeout_seconds is not None:
        valeurs["llm_timeout_seconds"] = payload.llm_timeout_seconds
    try:
        overrides.enregistrer(context.settings, valeurs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if context.llm is not None:
        await context.llm.aclose()
    context.llm = await build_llm(context.settings, context.hardware)
    return {
        "saved": True,
        "llm": context.llm.describe() if context.llm else {"backend": "extractive"},
    }


# ----------------------------------------------------------------- diagnostic
@router.get("/sessions")
async def sessions(context: AppContext = Depends(get_context)) -> dict:
    return {
        "sessions": [
            {
                "account": session.account,
                "is_admin": session.dsm.is_admin,
                "created_at": session.created_at,
                "last_seen": session.last_seen,
            }
            for session in context.sessions.all_sessions()
        ]
    }


@router.post("/dsm/reconnect")
async def reconnect(context: AppContext = Depends(get_context)) -> dict:
    # Action manuelle : on lève la suspension posée après un refus d'identifiants.
    ok = await context.refresh_service_session(force=True)
    return {
        "connected": ok,
        "shares_mapped": len(context.mapper.real_to_share),
        "mapping": context.mapper.real_to_share,
    }


@router.get("/whoami")
async def whoami(session: WebSession = Depends(require_admin)) -> dict:
    return {"account": session.account, "is_admin": True}
