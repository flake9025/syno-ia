"""Dépendances FastAPI communes : contexte applicatif, session et droits."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from ..security import COOKIE_NAME, WebSession
from ..services import AppContext


def get_context(request: Request) -> AppContext:
    context: AppContext | None = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - ne survient qu'au tout début du démarrage
        raise HTTPException(status_code=503, detail="Application en cours de démarrage")
    return context


def current_session(
    request: Request, context: AppContext = Depends(get_context)
) -> WebSession | None:
    return context.sessions.get(request.cookies.get(COOKIE_NAME))


def require_session(
    session: WebSession | None = Depends(current_session),
) -> WebSession:
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expirée ou absente, veuillez vous reconnecter.",
        )
    return session


def is_admin(session: WebSession, context: AppContext) -> bool:
    if session.dsm.is_admin:
        return True
    allowed = {account.lower() for account in context.settings.admin_accounts}
    return session.account.lower() in allowed


def require_admin(
    session: WebSession = Depends(require_session),
    context: AppContext = Depends(get_context),
) -> WebSession:
    if not is_admin(session, context):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cette action est réservée aux administrateurs du NAS.",
        )
    return session
