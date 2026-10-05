"""Contrôle d'accès par utilisateur, délégué à DSM.

Deux niveaux de filtrage sont appliqués, dans cet ordre :

1. **Filtre par partage** — les partages que l'utilisateur ne voit pas via
   `SYNO.FileStation.List/list_share` sont éliminés immédiatement (rapide,
   un seul appel mis en cache).
2. **Vérification fichier par fichier** — `SYNO.FileStation.List/getinfo` est
   appelé avec le `sid` de l'utilisateur sur les chemins candidats, ce qui
   applique les ACL avancées (permissions par sous-dossier, listes Windows ACL).

La stratégie est systématiquement *fail-closed* : en cas de doute ou d'erreur,
le document est masqué.

**Dossiers personnels.** DSM présente à chaque utilisateur son propre dossier
sous l'alias `/home`, tandis que le compte de service qui indexe voit le partage
parent `/homes`. Les deux niveaux ci-dessus traduisent donc `/homes/<compte>`
en `/home` pour le propriétaire du dossier — et pour lui seul. Le dossier d'un
tiers conserve son nom `/homes/<tiers>`, absent de la liste des partages d'un
utilisateur ordinaire, donc écarté dès le niveau 1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .client import DSMClient, DSMError
from .models import DSMSession, ShareInfo
from .paths import HOME_SHARE, PathMapper, home_prefix_for, normalize, to_personal_view

logger = logging.getLogger(__name__)

#: Nombre maximal de chemins envoyés à `getinfo` en un seul appel.
BATCH_SIZE = 20
#: Nombre maximal d'entrées conservées dans le cache d'autorisations.
MAX_CACHE_ENTRIES = 20_000


@dataclass
class _CacheEntry:
    value: bool
    expires_at: float


class AccessController:
    """Décide, pour une session utilisateur, quels chemins sont consultables."""

    def __init__(self, client: DSMClient, *, ttl: int = 300, strict: bool = True) -> None:
        self._client = client
        self._ttl = ttl
        self._strict = strict
        self._path_cache: dict[tuple[str, str], _CacheEntry] = {}
        self._share_cache: dict[str, tuple[float, list[ShareInfo]]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- partages
    async def shares_for(self, session: DSMSession) -> list[ShareInfo]:
        """Partages visibles par l'utilisateur (mis en cache pendant `ttl`)."""
        cached = self._share_cache.get(session.sid)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        try:
            shares = await self._client.list_shares(session.sid)
        except DSMError as exc:
            if exc.is_session_expired:
                raise
            logger.warning("Liste des partages indisponible pour %s : %s", session.account, exc)
            shares = []
        self._share_cache[session.sid] = (now + self._ttl, shares)
        return shares

    async def readable_share_paths(self, session: DSMSession) -> set[str]:
        return {share.key for share in await self.shares_for(session) if share.readable}

    async def index_scope(self, session: DSMSession) -> tuple[set[str], str]:
        """Périmètre de l'utilisateur **tel qu'il est stocké dans l'index**.

        Retourne les partages visibles et, le cas échéant, le préfixe du dossier
        personnel. Ce dernier est nécessaire parce que l'index enregistre les
        documents personnels sous `/homes/<compte>` (vue du compte de service),
        alors que l'utilisateur ne connaît que l'alias `/home` : un filtrage sur
        les seuls partages les écarterait tous.
        """
        shares = await self.readable_share_paths(session)
        home_prefix = home_prefix_for(session.account) if HOME_SHARE in shares else ""
        return shares, home_prefix

    # -------------------------------------------------------------- filtrage
    async def filter_paths(self, session: DSMSession, dsm_paths: list[str]) -> set[str]:
        """Retourne le sous-ensemble de `dsm_paths` que l'utilisateur peut lire."""
        candidates = [normalize(path) for path in dict.fromkeys(dsm_paths) if path]
        if not candidates:
            return set()

        allowed_shares = await self.readable_share_paths(session)
        # DSM présente le dossier personnel de l'utilisateur sous l'alias « /home »,
        # alors que l'index le connaît sous « /homes/<compte> » (vue du compte de
        # service). On contrôle donc chaque chemin sous le nom que DSM montre à
        # *cet* utilisateur, tout en continuant à renvoyer le chemin indexé.
        views = {path: to_personal_view(path, session.account) for path in candidates}
        # Niveau 1 : le partage racine doit être visible.
        level1 = [
            path for path in candidates if PathMapper.share_of(views[path]).lower() in allowed_shares
        ]
        if not level1 or not self._strict:
            return set(level1)

        # Niveau 2 : vérification réelle par File Station, avec cache.
        now = time.monotonic()
        resolved: set[str] = set()
        to_check: list[str] = []
        for path in level1:
            entry = self._path_cache.get((session.sid, views[path]))
            if entry and entry.expires_at > now:
                if entry.value:
                    resolved.add(path)
            else:
                to_check.append(path)

        for batch_start in range(0, len(to_check), BATCH_SIZE):
            batch = to_check[batch_start : batch_start + BATCH_SIZE]
            # Plusieurs chemins indexés ne peuvent pas partager une même vue : la
            # traduction est injective, mais on reste défensif sur les doublons.
            by_view: dict[str, list[str]] = {}
            for path in batch:
                by_view.setdefault(views[path], []).append(path)
            try:
                verdicts = await self._client.stat_paths(session.sid, list(by_view))
            except DSMError as exc:
                if exc.is_session_expired:
                    raise
                logger.warning("Vérification ACL impossible : %s", exc)
                verdicts = dict.fromkeys(by_view, False)
            async with self._lock:
                self._evict_if_needed(len(by_view))
                for view, allowed in verdicts.items():
                    self._path_cache[(session.sid, view)] = _CacheEntry(
                        value=allowed, expires_at=now + self._ttl
                    )
            for view, allowed in verdicts.items():
                if allowed:
                    resolved.update(by_view.get(view, []))
        return resolved

    async def can_read(self, session: DSMSession, dsm_path: str) -> bool:
        return normalize(dsm_path) in await self.filter_paths(session, [dsm_path])

    # ----------------------------------------------------------------- cache
    def invalidate(self, sid: str) -> None:
        self._share_cache.pop(sid, None)
        for key in [key for key in self._path_cache if key[0] == sid]:
            self._path_cache.pop(key, None)

    def _evict_if_needed(self, incoming: int) -> None:
        if len(self._path_cache) + incoming <= MAX_CACHE_ENTRIES:
            return
        now = time.monotonic()
        for key, entry in list(self._path_cache.items()):
            if entry.expires_at <= now:
                self._path_cache.pop(key, None)
        overflow = len(self._path_cache) + incoming - MAX_CACHE_ENTRIES
        if overflow > 0:
            for key in list(self._path_cache)[:overflow]:
                self._path_cache.pop(key, None)
