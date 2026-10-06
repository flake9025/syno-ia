"""Authentification : ouverture et fermeture de session DSM."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..security import COOKIE_NAME, DEVICE_COOKIE_NAME, WebSession
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
    trust_device: bool | None = Field(
        default=None,
        description=(
            "true : mémoriser cet appareil pour ne plus demander le code 2FA ; "
            "false : l'oublier ; absent : ne rien changer"
        ),
    )


class LoginResponse(BaseModel):
    account: str
    is_admin: bool
    language: str
    shares: list[str]
    device_trusted: bool = False


def _serialize(
    session: WebSession,
    context: AppContext,
    shares: list[str],
    device_trusted: bool = False,
) -> LoginResponse:
    return LoginResponse(
        account=session.account,
        is_admin=is_admin(session, context),
        language=session.language,
        shares=shares,
        device_trusted=device_trusted,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    context: AppContext = Depends(get_context),
) -> LoginResponse:
    """Authentifie l'utilisateur auprès de DSM et ouvre une session web."""
    account = payload.account.strip()
    trust_enabled = context.settings.device_trust_days > 0
    known_device = (
        context.sessions.read_device(account, request.cookies.get(DEVICE_COOKIE_NAME))
        if trust_enabled
        else None
    )
    try:
        dsm_session = await context.client.login(
            account,
            payload.password,
            payload.otp_code,
            device_id=known_device,
            trust_device=bool(payload.trust_device) and trust_enabled,
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
        if known_device and exc.needs_otp:
            # Le jeton a été révoqué depuis DSM : inutile de le représenter.
            response.delete_cookie(DEVICE_COOKIE_NAME, path="/")
        raise HTTPException(
            status_code=status_code,
            detail=exc.message,
            headers={"X-DSM-Error": str(exc.code)},
        ) from exc

    token, session = context.sessions.create(dsm_session, language=payload.language or "fr")
    secure = request.url.scheme == "https"
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=context.settings.session_ttl_minutes * 60,
        path="/",
    )

    device_trusted = bool(known_device)
    if trust_enabled and payload.trust_device and dsm_session.device_id:
        response.set_cookie(
            DEVICE_COOKIE_NAME,
            context.sessions.sign_device(session.account, dsm_session.device_id),
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=context.settings.device_trust_days * 86400,
            path="/",
        )
        device_trusted = True
        logger.info("Appareil mémorisé pour %s", session.account)
    elif payload.trust_device is False:
        # Décocher la case est la façon d'oublier l'appareil ; une connexion
        # silencieuse (case absente) le conserve.
        response.delete_cookie(DEVICE_COOKIE_NAME, path="/")
        device_trusted = False

    shares = [share.path for share in await context.access.shares_for(dsm_session)]
    logger.info(
        "Connexion réussie : %s (admin=%s, %d partage(s) accessible(s))",
        session.account,
        dsm_session.is_admin,
        len(shares),
    )
    return _serialize(session, context, shares, device_trusted)


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
