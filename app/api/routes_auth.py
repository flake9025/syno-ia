"""Authentification : ouverture et fermeture de session DSM."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..security import COOKIE_NAME, WebSession
from ..services import AppContext
from ..synology.client import DSMError
from .deps import current_session, get_context, is_admin, require_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    account: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)
    otp_code: str | None = Field(default=None, max_length=16)
    language: str = Field(default="fr", max_length=8)


class LoginResponse(BaseModel):
    account: str
    is_admin: bool
    language: str
    shares: list[str]


def _serialize(session: WebSession, context: AppContext, shares: list[str]) -> LoginResponse:
    return LoginResponse(
        account=session.account,
        is_admin=is_admin(session, context),
        language=session.language,
        shares=shares,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    context: AppContext = Depends(get_context),
) -> LoginResponse:
    """Authentifie l'utilisateur auprès de DSM et ouvre une session web."""
    try:
        dsm_session = await context.client.login(
            payload.account.strip(), payload.password, payload.otp_code
        )
    except DSMError as exc:
        logger.info("Échec de connexion pour %s : %s", payload.account, exc)
        status_code = status.HTTP_401_UNAUTHORIZED
        if exc.needs_otp:
            status_code = status.HTTP_428_PRECONDITION_REQUIRED
        elif exc.code == 407:
            status_code = status.HTTP_429_TOO_MANY_REQUESTS
        elif exc.code == 100:
            status_code = status.HTTP_502_BAD_GATEWAY
        raise HTTPException(
            status_code=status_code,
            detail=exc.message,
            headers={"X-DSM-Error": str(exc.code)},
        ) from exc

    token, session = context.sessions.create(dsm_session, language=payload.language or "fr")
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=context.settings.session_ttl_minutes * 60,
        path="/",
    )
    shares = [share.path for share in await context.access.shares_for(dsm_session)]
    logger.info(
        "Connexion réussie : %s (admin=%s, %d partage(s) accessible(s))",
        session.account,
        dsm_session.is_admin,
        len(shares),
    )
    return _serialize(session, context, shares)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    context: AppContext = Depends(get_context),
) -> dict:
    session = context.sessions.pop(request.cookies.get(COOKIE_NAME))
    if session is not None:
        context.access.invalidate(session.dsm.sid)
        await context.client.logout(session.dsm.sid)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(
    session: WebSession | None = Depends(current_session),
    context: AppContext = Depends(get_context),
) -> dict:
    """Retourne l'utilisateur courant, ou `authenticated=false` si la session est absente."""
    if session is None:
        return {"authenticated": False, "language": context.settings.default_language}
    shares = [share.path for share in await context.access.shares_for(session.dsm)]
    return {
        "authenticated": True,
        "account": session.account,
        "is_admin": is_admin(session, context),
        "language": session.language,
        "shares": shares,
    }


@router.get("/shares")
async def shares(
    session: WebSession = Depends(require_session),
    context: AppContext = Depends(get_context),
) -> dict:
    """Partages réellement accessibles à l'utilisateur (périmètre de son RAG)."""
    accessible = await context.access.shares_for(session.dsm)
    indexed = {entry["share"] for entry in context.store.stats()["shares"]}
    return {
        "shares": [
            {
                "path": share.path,
                "name": share.name,
                "indexed": share.key in indexed,
            }
            for share in accessible
        ]
    }
