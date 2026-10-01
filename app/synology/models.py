"""Objets de transfert liés à DSM."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DSMSession:
    """Session DSM authentifiée pour un utilisateur donné."""

    sid: str
    account: str
    is_admin: bool = False
    device_id: str = ""

    def redacted(self) -> dict:
        return {"account": self.account, "is_admin": self.is_admin}


@dataclass
class ShareInfo:
    """Partage DSM visible par un utilisateur."""

    #: Chemin virtuel DSM, ex. « /documents ».
    path: str
    name: str
    #: Chemin réel sur le NAS, ex. « /volume1/documents » (si l'API le fournit).
    real_path: str = ""
    readable: bool = True
    writable: bool = False

    @property
    def key(self) -> str:
        return self.path.rstrip("/").lower()


@dataclass
class RemoteFile:
    """Entrée de fichier retournée par File Station."""

    path: str
    name: str
    is_dir: bool
    size: int = 0
    mtime: int = 0
    extra: dict = field(default_factory=dict)
