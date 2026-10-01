"""Assemblage des composants de l'application (conteneur d'injection simplifié)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .hardware import HardwareProfile, detect_hardware
from .llm.base import LLMBackend
from .llm.factory import build_llm, local_model_path
from .rag.embedder import Embedder, build_embedder
from .rag.indexer import Indexer, IndexerConfig
from .rag.pipeline import RetrievalPipeline
from .rag.store import DocumentStore
from .security import SessionStore
from .synology.acl import AccessController
from .synology.client import DSMClient, DSMError
from .synology.models import DSMSession
from .synology.paths import PathMapper

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Toutes les dépendances partagées, construites une fois au démarrage."""

    settings: Settings
    hardware: HardwareProfile
    store: DocumentStore
    embedder: Embedder
    client: DSMClient
    access: AccessController
    sessions: SessionStore
    pipeline: RetrievalPipeline
    indexer: Indexer
    llm: LLMBackend | None = None
    mapper: PathMapper = field(default_factory=PathMapper.empty)
    service_session: DSMSession | None = None
    started_at: float = field(default_factory=time.time)
    startup_errors: list[str] = field(default_factory=list)
    _scheduler: asyncio.Task | None = None
    _service_login_blocked: bool = False

    # ---------------------------------------------------------------- statut
    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    def status(self) -> dict:
        return {
            "app": self.settings.app_name,
            "version": self.settings.app_version,
            "build": {"sha": self.settings.build_sha, "date": self.settings.build_date},
            "uptime": round(self.uptime, 1),
            "hardware": self.hardware.to_dict(),
            "embedder": self.embedder.describe(),
            "llm": self.llm.describe() if self.llm else {"backend": "extractive", "available": True},
            "index": self.store.stats(),
            "indexing": self.indexer.progress.snapshot(),
            "dsm": {
                "url": self.settings.dsm_url,
                "service_account": bool(self.service_session),
                "shares_mapped": len(self.mapper.real_to_share),
                "mode": self.settings.index_mode,
                "acl_strict": self.settings.acl_strict,
            },
            "sessions": len(self.sessions),
            "startup_errors": self.startup_errors,
        }

    # ------------------------------------------------------------- cycle vie
    async def refresh_service_session(self, *, force: bool = False) -> bool:
        """(Re)connecte le compte de service et met à jour la carte des partages.

        DSM bloque l'adresse IP source après quelques échecs d'authentification
        (Auto Block). Comme tous les conteneurs du bridge partagent la même IP,
        une boucle de reconnexion sur des identifiants erronés bloquerait tout le
        sous-réseau Docker : après un refus d'identifiants, on n'insiste pas tant
        qu'un administrateur n'a pas relancé l'opération manuellement.
        """
        if not self.settings.has_service_account:
            return False
        if self._service_login_blocked and not force:
            logger.debug("Reconnexion du compte de service suspendue (échec précédent)")
            return False
        try:
            session = await self.client.login(
                self.settings.dsm_service_account, self.settings.dsm_service_password
            )
        except DSMError as exc:
            message = f"Compte de service DSM refusé : {exc}"
            logger.error(message)
            self._remember_error(message)
            self._service_login_blocked = exc.is_auth_failure or exc.is_app_privilege
            return False

        self._service_login_blocked = False
        self.service_session = session
        try:
            shares = await self.client.list_shares(session.sid)
            self.mapper = PathMapper.from_shares(shares)
            self.indexer.mapper = self.mapper
            self.indexer.service_session = session
            logger.info(
                "Compte de service connecté (%s) — %d partage(s) cartographié(s)",
                session.account,
                len(self.mapper.real_to_share),
            )
        except DSMError as exc:
            logger.warning("Cartographie des partages impossible : %s", exc)
        return True

    async def start_scheduler(self) -> None:
        if self.settings.index_interval_minutes <= 0:
            return
        self._scheduler = asyncio.create_task(self._schedule_loop())

    async def _schedule_loop(self) -> None:
        interval = self.settings.index_interval_minutes * 60
        while True:
            try:
                await asyncio.sleep(interval)
                if not self.indexer.running:
                    logger.info("Réindexation périodique déclenchée")
                    await self.refresh_service_session()
                    await self.indexer.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - boucle de fond
                logger.exception("Erreur du planificateur d'indexation : %s", exc)

    async def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
        await self.indexer.cancel()
        if self.service_session:
            await self.client.logout(self.service_session.sid)
        if self.llm:
            await self.llm.aclose()
        await self.client.aclose()
        self.store.close()

    def _remember_error(self, message: str) -> None:
        if message not in self.startup_errors:
            self.startup_errors.append(message)


async def build_context(settings: Settings | None = None) -> AppContext:
    """Construit le contexte applicatif complet."""
    settings = settings or get_settings()
    hardware = detect_hardware(settings.hardware_profile)

    # Les paramètres de récupération suivent le profil, sauf surcharge explicite.
    tuning = hardware.tuning()
    candidates = settings.retrieval_candidates or tuning["retrieval_candidates"]
    top_k = settings.retrieval_top_k or tuning["retrieval_top_k"]
    context_chars = settings.context_max_chars or tuning["context_max_chars"]

    store = DocumentStore(settings.db_path)

    backend, model = hardware.embedding_choice()
    embedder = build_embedder(
        settings.embedding_backend if settings.embedding_backend != "auto" else backend,
        settings.embedding_model or model,
        cache_dir=settings.models_dir,
        threads=settings.llm_threads or hardware.recommended_threads(),
    )

    client = DSMClient(
        settings.dsm_url,
        verify_ssl=settings.dsm_verify_ssl,
        timeout=settings.dsm_timeout,
        session_name=settings.dsm_session_name,
    )
    access = AccessController(client, ttl=settings.acl_cache_ttl, strict=settings.acl_strict)
    sessions = SessionStore(settings.app_secret, ttl_minutes=settings.session_ttl_minutes)
    pipeline = RetrievalPipeline(
        store,
        embedder,
        access,
        candidates=candidates,
        top_k=top_k,
        context_max_chars=context_chars,
    )
    indexer = Indexer(
        store,
        embedder,
        IndexerConfig(
            roots=settings.index_roots,
            exclude_globs=settings.index_exclude_globs,
            max_file_mb=settings.index_max_file_mb,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            batch_size=settings.index_batch_size,
            mode=settings.index_mode,
        ),
        client=client,
    )

    context = AppContext(
        settings=settings,
        hardware=hardware,
        store=store,
        embedder=embedder,
        client=client,
        access=access,
        sessions=sessions,
        pipeline=pipeline,
        indexer=indexer,
    )

    for warning in hardware.warnings:
        context._remember_error(warning)

    await context.refresh_service_session()
    context.llm = await build_llm(settings, hardware)
    if context.llm is None:
        expected = local_model_path(settings, hardware)
        logger.info(
            "Mode extractif actif. Pour activer la génération : installez Ollama, "
            "renseignez OPENAI_API_KEY, ou téléchargez %s",
            expected.name if expected else "un modèle GGUF",
        )
    return context
