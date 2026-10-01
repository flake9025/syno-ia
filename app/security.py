"""Sessions web adossées aux sessions DSM.

Le `sid` DSM ne quitte jamais le serveur : le navigateur ne reçoit qu'un jeton
opaque signé (HMAC) stocké dans un cookie `HttpOnly`/`SameSite=Lax`. Les
sessions sont conservées en mémoire : un redémarrage du conteneur impose une
reconnexion, ce qui est volontaire (aucun identifiant persisté sur disque).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

from .synology.models import DSMSession

COOKIE_NAME = "syno_ia_session"


@dataclass
class WebSession:
    token: str
    dsm: DSMSession
    created_at: float
    expires_at: float
    last_seen: float
    last_validated: float = 0.0
    language: str = "fr"
    history: list[dict] = field(default_factory=list)

    @property
    def account(self) -> str:
        return self.dsm.account

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at


class SessionStore:
    """Registre en mémoire des sessions web actives."""

    def __init__(self, secret: str, ttl_minutes: int = 720) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl = max(5, ttl_minutes) * 60
        self._sessions: dict[str, WebSession] = {}

    # ------------------------------------------------------------- signature
    def _sign(self, token: str) -> str:
        digest = hmac.new(self._secret, token.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        return f"{token}.{digest}"

    def _verify(self, signed: str) -> str | None:
        if not signed or "." not in signed:
            return None
        token, _, digest = signed.rpartition(".")
        if not token or not digest:
            return None
        expected = hmac.new(self._secret, token.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        return token if hmac.compare_digest(expected, digest) else None

    # -------------------------------------------------------------- lifecycle
    def create(self, dsm: DSMSession, language: str = "fr") -> tuple[str, WebSession]:
        self.purge()
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = WebSession(
            token=token,
            dsm=dsm,
            created_at=now,
            expires_at=now + self._ttl,
            last_seen=now,
            language=language,
        )
        self._sessions[token] = session
        return self._sign(token), session

    def get(self, signed_token: str | None) -> WebSession | None:
        if not signed_token:
            return None
        token = self._verify(signed_token)
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.is_expired():
            self._sessions.pop(token, None)
            return None
        session.last_seen = time.time()
        return session

    def pop(self, signed_token: str | None) -> WebSession | None:
        if not signed_token:
            return None
        token = self._verify(signed_token)
        if not token:
            return None
        return self._sessions.pop(token, None)

    def purge(self) -> int:
        now = time.time()
        expired = [token for token, session in self._sessions.items() if session.is_expired(now)]
        for token in expired:
            self._sessions.pop(token, None)
        return len(expired)

    def all_sessions(self) -> list[WebSession]:
        self.purge()
        return list(self._sessions.values())

    def __len__(self) -> int:
        return len(self._sessions)
