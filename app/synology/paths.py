"""Correspondance entre chemins réels du NAS et chemins virtuels DSM.

En mode « mount », les partages sont montés dans le conteneur avec le même
chemin que sur le NAS (`/volume1/documents` → `/volume1/documents`). L'index
stocke donc des chemins réels, tandis que les contrôles de permission passent
par File Station qui raisonne en chemins virtuels (`/documents`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .models import ShareInfo

logger = logging.getLogger(__name__)

_VOLUME_RE = re.compile(r"^/volume(?:USB)?\d+(?:/|$)", re.IGNORECASE)


def normalize(path: str) -> str:
    """Normalise un chemin POSIX (séparateurs, doublons, slash final)."""
    if not path:
        return ""
    cleaned = path.replace("\\", "/")
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    if len(cleaned) > 1:
        cleaned = cleaned.rstrip("/")
    return cleaned


@dataclass
class PathMapper:
    """Traduit les chemins réels en chemins DSM et inversement."""

    #: {chemin réel normalisé en minuscules: chemin DSM}
    real_to_share: dict[str, str]

    @classmethod
    def from_shares(cls, shares: list[ShareInfo]) -> PathMapper:
        mapping: dict[str, str] = {}
        for share in shares:
            if share.real_path and share.path:
                mapping[normalize(share.real_path).lower()] = normalize(share.path)
        return cls(real_to_share=mapping)

    @classmethod
    def empty(cls) -> PathMapper:
        return cls(real_to_share={})

    def to_dsm(self, real_path: str) -> str:
        """`/volume1/documents/rh/contrat.pdf` → `/documents/rh/contrat.pdf`."""
        target = normalize(real_path)
        lowered = target.lower()
        best: tuple[int, str, str] | None = None
        for real_root, share_path in self.real_to_share.items():
            if (lowered == real_root or lowered.startswith(real_root + "/")) and (
                best is None or len(real_root) > best[0]
            ):
                best = (len(real_root), real_root, share_path)
        if best is not None:
            remainder = target[best[0] :]
            return normalize(best[2] + remainder)

        # Repli : retirer le préfixe de volume (`/volume1/...` → `/...`).
        match = _VOLUME_RE.match(target)
        if match:
            return normalize("/" + target[match.end() :].lstrip("/"))
        return target

    def to_real(self, dsm_path: str) -> str:
        """`/documents/rh/contrat.pdf` → `/volume1/documents/rh/contrat.pdf` si connu."""
        target = normalize(dsm_path)
        lowered = target.lower()
        best: tuple[int, str, str] | None = None
        for real_root, share_path in self.real_to_share.items():
            share_lower = share_path.lower()
            if (lowered == share_lower or lowered.startswith(share_lower + "/")) and (
                best is None or len(share_lower) > best[0]
            ):
                best = (len(share_lower), share_lower, real_root)
        if best is None:
            return target
        # `best[2]` conserve la casse d'origine du chemin réel.
        return normalize(best[2] + target[best[0] :])

    @staticmethod
    def share_of(dsm_path: str) -> str:
        """Retourne le partage racine d'un chemin DSM (`/documents/rh/x` → `/documents`)."""
        normalized = normalize(dsm_path)
        parts = [p for p in normalized.split("/") if p]
        return "/" + parts[0] if parts else ""
