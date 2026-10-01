"""Indexation des documents : parcours, extraction, découpage et vectorisation.

Deux modes de lecture sont disponibles :

* **mount** — les partages sont montés dans le conteneur (`-v /volume1/docs:/volume1/docs:ro`).
  Lecture directe sur le système de fichiers : c'est le mode le plus rapide.
* **filestation** — aucun montage n'est nécessaire, les fichiers sont parcourus
  et téléchargés via l'API File Station avec le compte de service.

Dans les deux cas, l'indexation est **incrémentale** : un document n'est
retraité que si sa taille ou sa date de modification a changé.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..synology.client import DSMClient, DSMError
from ..synology.models import DSMSession
from ..synology.paths import PathMapper, normalize
from .chunker import TextSegment, chunk_segments
from .embedder import Embedder
from .extract import UnsupportedDocument, extension_of, extract, is_supported
from .store import Chunk, DocumentRecord, DocumentStore

logger = logging.getLogger(__name__)


@dataclass
class IndexProgress:
    """État courant de l'indexation, exposé à l'interface d'administration."""

    status: str = "idle"  # idle | running | error | cancelled
    started_at: float = 0.0
    finished_at: float = 0.0
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    removed: int = 0
    total: int = 0
    current: str = ""
    last_error: str = ""
    last_run_at: float = 0.0
    last_duration: float = 0.0

    def snapshot(self) -> dict:
        data = self.__dict__.copy()
        data["running"] = self.status == "running"
        data["elapsed"] = (
            (time.time() - self.started_at) if self.status == "running" and self.started_at else self.last_duration
        )
        return data


@dataclass
class _FileEntry:
    real_path: str
    name: str
    size: int
    mtime: int


@dataclass
class IndexerConfig:
    roots: list[str]
    exclude_globs: list[str] = field(default_factory=list)
    max_file_mb: float = 40.0
    chunk_size: int = 900
    chunk_overlap: int = 150
    batch_size: int = 32
    mode: str = "mount"


class Indexer:
    """Orchestre la construction de l'index documentaire."""

    def __init__(
        self,
        store: DocumentStore,
        embedder: Embedder,
        config: IndexerConfig,
        *,
        client: DSMClient | None = None,
        mapper: PathMapper | None = None,
        service_session: DSMSession | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.config = config
        self.client = client
        self.mapper = mapper or PathMapper.empty()
        self.service_session = service_session
        self.progress = IndexProgress()
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- pilotage
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, full: bool = False) -> bool:
        """Lance une indexation en arrière-plan. Retourne False si déjà en cours."""
        async with self._lock:
            if self.running:
                return False
            self._cancel.clear()
            self._task = asyncio.create_task(self._run(full=full))
            return True

    async def cancel(self) -> None:
        self._cancel.set()

    async def wait(self) -> None:
        if self._task:
            await asyncio.shield(self._task)

    # ------------------------------------------------------------ exécution
    async def _run(self, *, full: bool) -> None:
        self.progress = IndexProgress(status="running", started_at=time.time())
        started = time.perf_counter()
        try:
            if full:
                await asyncio.to_thread(self.store.clear)
            entries = await self._collect_entries()
            self.progress.total = len(entries)

            seen: list[str] = []
            pending = 0
            for entry in entries:
                if self._cancel.is_set():
                    self.progress.status = "cancelled"
                    break
                self.progress.scanned += 1
                self.progress.current = entry.real_path
                seen.append(entry.real_path)

                fingerprint = await asyncio.to_thread(
                    self.store.document_fingerprint, entry.real_path
                )
                if not full and fingerprint == (entry.size, entry.mtime):
                    self.progress.skipped += 1
                    continue

                try:
                    await self._index_entry(entry)
                    self.progress.indexed += 1
                except Exception as exc:  # une erreur isolée ne doit pas tout arrêter
                    self.progress.failed += 1
                    self.progress.last_error = f"{entry.name}: {exc}"
                    logger.warning("Indexation échouée pour %s : %s", entry.real_path, exc)
                    await asyncio.to_thread(self._record_failure, entry, str(exc))

                pending += 1
                if pending >= self.config.batch_size:
                    pending = 0
                    await asyncio.sleep(0)  # rend la main à la boucle d'événements

            if not self._cancel.is_set():
                self.progress.removed = await asyncio.to_thread(
                    self.store.delete_missing, seen, self.config.roots
                )
                self.progress.status = "idle"
        except Exception as exc:
            self.progress.status = "error"
            self.progress.last_error = str(exc)
            logger.exception("Indexation interrompue : %s", exc)
        finally:
            self.progress.current = ""
            self.progress.finished_at = time.time()
            self.progress.last_run_at = self.progress.finished_at
            self.progress.last_duration = time.perf_counter() - started
            self.store.set_meta("last_index_at", str(self.progress.finished_at))
            logger.info(
                "Indexation terminée : %d indexés, %d inchangés, %d en erreur, %d supprimés (%.1fs)",
                self.progress.indexed,
                self.progress.skipped,
                self.progress.failed,
                self.progress.removed,
                self.progress.last_duration,
            )

    # ------------------------------------------------------------- collecte
    async def _collect_entries(self) -> list[_FileEntry]:
        if self.config.mode == "filestation":
            return await self._collect_via_filestation()
        return await asyncio.to_thread(self._collect_via_mount)

    def _collect_via_mount(self) -> list[_FileEntry]:
        entries: list[_FileEntry] = []
        for root in self.config.roots:
            root_path = Path(root)
            if not root_path.exists():
                logger.warning("Racine d'indexation absente du conteneur : %s", root)
                continue
            for item in self._walk(root_path):
                try:
                    stat = item.stat()
                except OSError:
                    continue
                entries.append(
                    _FileEntry(
                        real_path=normalize(str(item.as_posix())),
                        name=item.name,
                        size=int(stat.st_size),
                        mtime=int(stat.st_mtime),
                    )
                )
        return entries

    def _walk(self, root: Path) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            posix_dir = Path(dirpath).as_posix()
            dirnames[:] = [
                name
                for name in dirnames
                if not self._excluded(f"{posix_dir}/{name}") and name not in {"@eaDir", "#recycle"}
            ]
            for filename in filenames:
                candidate = f"{posix_dir}/{filename}"
                if self._excluded(candidate) or not is_supported(filename):
                    continue
                yield Path(candidate)

    async def _collect_via_filestation(self) -> list[_FileEntry]:
        if not (self.client and self.service_session):
            raise RuntimeError(
                "Le mode « filestation » exige un compte de service DSM (DSM_SERVICE_ACCOUNT)."
            )
        entries: list[_FileEntry] = []
        for root in self.config.roots:
            dsm_root = self.mapper.to_dsm(root) if root.startswith("/volume") else normalize(root)
            async for remote in self.client.walk(self.service_session.sid, dsm_root):
                if not is_supported(remote.name) or self._excluded(remote.path):
                    continue
                entries.append(
                    _FileEntry(
                        real_path=normalize(remote.extra.get("real_path") or remote.path),
                        name=remote.name,
                        size=remote.size,
                        mtime=remote.mtime,
                    )
                )
        return entries

    def _excluded(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.config.exclude_globs)

    # ------------------------------------------------------------- traitement
    async def _index_entry(self, entry: _FileEntry) -> None:
        max_bytes = int(self.config.max_file_mb * 1024 * 1024)
        if entry.size > max_bytes:
            raise UnsupportedDocument(
                f"Fichier trop volumineux ({entry.size / 1048576:.1f} Mo > {self.config.max_file_mb} Mo)"
            )

        data = await self._read(entry)
        segments = await asyncio.to_thread(extract, entry.name, data)
        chunks = await asyncio.to_thread(
            chunk_segments,
            segments,
            chunk_size=self.config.chunk_size,
            overlap=self.config.chunk_overlap,
        )
        if not chunks:
            raise UnsupportedDocument("Aucun texte exploitable dans ce document")

        vectors: list = [None] * len(chunks)
        if self.embedder.available:
            vectors = await asyncio.to_thread(
                self.embedder.embed_documents, [chunk.text for chunk in chunks]
            )

        record = self._record_for(entry, data)
        payload = [
            Chunk(text=chunk.text, ordinal=index, location=chunk.location, embedding=vectors[index])
            for index, chunk in enumerate(chunks)
        ]
        await asyncio.to_thread(self.store.upsert_document, record, payload)

    async def _read(self, entry: _FileEntry) -> bytes:
        if self.config.mode == "filestation":
            if not (self.client and self.service_session):
                raise RuntimeError("Compte de service DSM requis en mode « filestation ».")
            dsm_path = self.mapper.to_dsm(entry.real_path)
            try:
                return await self.client.download(self.service_session.sid, dsm_path)
            except DSMError as exc:
                raise UnsupportedDocument(f"Téléchargement impossible : {exc}") from exc
        return await asyncio.to_thread(Path(entry.real_path).read_bytes)

    def _record_for(self, entry: _FileEntry, data: bytes, *, status: str = "ok", error: str = "") -> DocumentRecord:
        dsm_path = self.mapper.to_dsm(entry.real_path)
        return DocumentRecord(
            real_path=entry.real_path,
            dsm_path=dsm_path,
            share=PathMapper.share_of(dsm_path).lower(),
            name=entry.name,
            ext=extension_of(entry.name),
            size=entry.size,
            mtime=entry.mtime,
            content_hash=hashlib.sha256(data).hexdigest()[:32] if data else "",
            status=status,
            error=error,
        )

    def _record_failure(self, entry: _FileEntry, error: str) -> None:
        """Mémorise l'échec pour éviter de retenter le fichier à chaque passage."""
        record = self._record_for(entry, b"", status="error", error=error[:500])
        self.store.upsert_document(record, [])


def segments_preview(segments: list[TextSegment], limit: int = 3) -> list[str]:
    """Utilitaire de diagnostic : aperçu des premiers segments extraits."""
    return [segment.text[:200] for segment in segments[:limit]]
