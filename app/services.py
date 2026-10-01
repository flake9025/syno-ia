"""Assemblage des composants de l'application (conteneur d'injection simplifié)."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .hardware import HardwareProfile, detect_hardware
from .llm.base import LLMBackend
from .llm.factory import build_llm, local_model_path
from .rag.backfill import VectorBackfiller
from .rag.embedder import DeferredEmbedder, Embedder, NullEmbedder, build_embedder
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
    backfiller: VectorBackfiller
    llm: LLMBackend | None = None
    #: (moteur, modèle, threads) à charger en arrière-plan.
    embedding_target: tuple[str, str, int] = ("none", "", 0)
    mapper: PathMapper = field(default_factory=PathMapper.empty)
    service_session: DSMSession | None = None
    started_at: float = field(default_factory=time.time)
    startup_errors: list[str] = field(default_factory=list)
    _scheduler: asyncio.Task | None = None
    _warmup: asyncio.Task | None = None
    _service_login_blocked: bool = False

    # ---------------------------------------------------------------- statut
    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    @property
    def embeddings_pending(self) -> bool:
        """Le moteur sémantique est-il encore en cours de mise à disposition ?

        Vrai pendant le chargement du modèle comme pendant le rattrapage des
        vecteurs : dans les deux cas la recherche est temporairement dégradée,
        et le dire évite de faire passer un démarrage normal pour une panne.
        """
        if getattr(self.embedder, "state", "ready") in {"pending", "loading"}:
            return True
        return self.backfiller.progress.status == "running"

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
            "vectorization": self.backfiller.progress.snapshot(),
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

    # -------------------------------------------------------- embeddings
    def start_embeddings(self) -> bool:
        """Charge le moteur d'embeddings en arrière-plan, puis complète l'index.

        Sans attendre : l'application sert déjà des réponses en BM25 pendant le
        téléchargement du modèle, qui peut durer de longues minutes au premier
        démarrage.
        """
        if not isinstance(self.embedder, DeferredEmbedder):
            return False
        if self._warmup and not self._warmup.done():
            return False
        self._warmup = asyncio.create_task(self._load_embeddings())
        return True

    async def _load_embeddings(self) -> None:
        deferred = self.embedder
        assert isinstance(deferred, DeferredEmbedder)
        backend, model, threads = self.embedding_target
        deferred.mark_loading()
        logger.info(
            "Chargement du moteur d'embeddings en arrière-plan (%s / %s) — "
            "la recherche reste lexicale jusqu'à sa mise à disposition",
            backend,
            model or "modèle par défaut",
        )
        try:
            engine = await asyncio.to_thread(
                build_embedder,
                backend,
                model,
                cache_dir=self.settings.models_dir,
                threads=threads,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"Moteur d'embeddings indisponible : {exc}"
            logger.error(message)
            self._remember_error(message)
            deferred.adopt(NullEmbedder(str(exc)))
            return

        deferred.adopt(engine)
        if not engine.available:
            self._remember_error(
                "Aucun moteur d'embeddings n'a pu être chargé — recherche lexicale seule (BM25)."
            )
            return

        await self.backfiller.run()


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
        self.backfiller.cancel()
        if self._warmup:
            self._warmup.cancel()
            # Le chargement s'exécute dans un thread : on laisse la tâche se
            # dénouer plutôt que d'abandonner la boucle sur une exception.
            with suppress(asyncio.CancelledError):
                await self._warmup
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
    embedding_backend = settings.embedding_backend if settings.embedding_backend != "auto" else backend
    embedding_model = settings.embedding_model or model
    threads = settings.llm_threads or hardware.recommended_threads()

    # Le chargement du moteur (téléchargement compris) est différé : l'application
    # doit être joignable tout de suite, quitte à ne faire que du BM25 au début.
    if settings.embedding_async_load and embedding_backend.lower() != "none":
        embedder: Embedder = DeferredEmbedder()
    else:
        embedder = build_embedder(
            embedding_backend, embedding_model, cache_dir=settings.models_dir, threads=threads
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
        backfiller=VectorBackfiller(
            store,
            embedder,
            batch_size=settings.embedding_backfill_batch,
            is_indexing=lambda: indexer.running,
        ),
        embedding_target=(embedding_backend, embedding_model, threads),
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
