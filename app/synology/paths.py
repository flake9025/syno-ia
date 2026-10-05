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

#: Partage DSM regroupant les dossiers personnels, visible des seuls administrateurs.
HOMES_SHARE = "/homes"
#: Alias sous lequel DSM présente à chaque utilisateur son propre dossier personnel.
HOME_SHARE = "/home"


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


def home_owner(dsm_path: str) -> str:
    """Compte propriétaire d'un chemin situé sous `/homes`, sinon chaîne vide."""
    parts = [part for part in normalize(dsm_path).split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == HOMES_SHARE.strip("/"):
        return parts[1]
    return ""


def home_prefix_for(account: str) -> str:
    """Chemin sous lequel l'index connaît le dossier personnel d'un compte."""
    account = (account or "").strip().strip("/")
    return f"{HOMES_SHARE}/{account}" if account else ""


def to_personal_view(dsm_path: str, account: str) -> str:
    """Traduit `/homes/<compte>/x` en `/home/x` pour son propriétaire.

    DSM ne présente jamais `/homes` à un utilisateur ordinaire : son dossier
    personnel lui apparaît sous l'alias `/home`. Un document indexé sous
    `/homes/alice/notes.pdf` doit donc être vérifié sous le nom
    `/home/notes.pdf` quand c'est Alice qui interroge, faute de quoi le partage
    racine serait introuvable dans sa liste et le document masqué à tort.

    Tout autre chemin est renvoyé inchangé : le dossier personnel d'un tiers
    reste donc sous `/homes/<tiers>`, que seuls les administrateurs voient.
    """
    owner = home_owner(dsm_path)
    if not owner or not account or owner.lower() != account.lower():
        return normalize(dsm_path)
    remainder = [part for part in normalize(dsm_path).split("/") if part][2:]
    return normalize("/".join([HOME_SHARE, *remainder]))


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
