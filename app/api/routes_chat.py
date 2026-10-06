"""Interrogation des documents : recherche, génération en flux et téléchargement."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..llm.base import LLMUnavailable
from ..llm.extractive import stream_extractive_answer
from ..llm.prompts import build_messages, no_context_message
from ..security import WebSession
from ..services import AppContext
from ..synology.client import DSMError
from .deps import get_context, require_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])

MAX_QUESTION_LENGTH = 2000

#: Silence maximal toléré dans le flux SSE. Sur un NAS lent, le chargement du modèle
#: puis l'analyse du contexte s'écoulent sans produire le moindre jeton ; un flux muet
#: aussi longtemps est fermé par tout intermédiaire et ne rassure pas l'utilisateur.
HEARTBEAT_SECONDS = 15.0

#: Commentaire SSE : dépourvu de champ « data », il est ignoré par le client mais
#: suffit à maintenir la connexion ouverte et à prouver que le serveur travaille.
_HEARTBEAT = ": ping\n\n"

#: Message affiché lorsque la génération est arrêtée faute de temps.
TRUNCATED_NOTICE = (
    "Réponse interrompue : délai de génération dépassé. Les sources citées restent complètes."
)


@dataclass
class _Timing:
    """Chronomètre d'une réponse, pour que l'utilisateur sache où part le temps."""

    started: float = field(default_factory=time.monotonic)
    retrieval_ms: int = 0
    first_token_ms: int = 0
    tokens: int = 0
    truncated: bool = False

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def snapshot(self) -> dict:
        data = {
            "retrieval_ms": self.retrieval_ms,
            "first_token_ms": self.first_token_ms,
            "total_ms": self.elapsed_ms(),
            "tokens": self.tokens,
            "truncated": self.truncated,
        }
        generation_ms = data["total_ms"] - self.first_token_ms
        if self.tokens > 1 and generation_ms > 0:
            data["tokens_per_second"] = round(self.tokens / (generation_ms / 1000), 1)
        return data


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    history: list[dict] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=20)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_tokens(
    tokens: AsyncIterator[str],
    answer_parts: list[str],
    timing: _Timing,
    deadline: float | None = None,
) -> AsyncIterator[str]:
    """Relaie les jetons en SSE, en intercalant un battement de cœur pendant les silences.

    Passé `deadline`, la génération est abandonnée et ce qui a déjà été produit est
    conservé : sur un NAS modeste, une réponse tronquée vaut mieux qu'une requête
    qui n'aboutit jamais.
    """
    iterator = tokens.__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                timing.truncated = True
                logger.warning("Génération interrompue : délai de %s dépassé", deadline)
                return
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            wait_for = HEARTBEAT_SECONDS
            if deadline is not None:
                wait_for = max(0.1, min(wait_for, deadline - time.monotonic()))
            done, _ = await asyncio.wait({pending}, timeout=wait_for)
            if not done:
                yield _HEARTBEAT
                continue
            finished, pending = pending, None
            try:
                token = finished.result()
            except StopAsyncIteration:
                return
            if not timing.tokens:
                timing.first_token_ms = timing.elapsed_ms()
            timing.tokens += 1
            answer_parts.append(token)
            yield _sse("token", {"text": token})
    finally:
        if pending is not None:
            pending.cancel()
            if pending.done() and not pending.cancelled():
                pending.exception()


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    session: WebSession = Depends(require_session),
    context: AppContext = Depends(get_context),
) -> StreamingResponse:
    """Répond à une question en flux SSE : `sources`, puis `token`, puis `done`."""
    question = payload.question.strip()
    language = session.language or context.settings.default_language

    def _engine() -> dict:
        """Identité du moteur qui va répondre, pour que l'utilisateur puisse la vérifier."""
        if context.llm is not None and context.llm.available:
            info = context.llm.describe()
            return {
                "backend": info.get("backend", "?"),
                "model": info.get("model") or "?",
                "threads": info.get("threads"),
                "context_size": info.get("context_size"),
            }
        return {"backend": "extractive", "model": None}

    async def event_stream() -> AsyncIterator[str]:
        answer_parts: list[str] = []
        timing = _Timing()
        retrieval = asyncio.ensure_future(
            context.pipeline.retrieve(session.dsm, question, top_k=payload.top_k)
        )
        try:
            while True:
                done, _ = await asyncio.wait({retrieval}, timeout=HEARTBEAT_SECONDS)
                if done:
                    break
                yield _HEARTBEAT
            result = retrieval.result()
        except DSMError as exc:
            yield _sse("error", {"message": f"DSM : {exc.message}", "code": exc.code})
            return
        except Exception as exc:  # pragma: no cover - garde-fou
            logger.exception("Échec de la récupération : %s", exc)
            yield _sse("error", {"message": "Erreur interne pendant la recherche."})
            return
        finally:
            retrieval.cancel()

        timing.retrieval_ms = timing.elapsed_ms()
        engine = _engine()
        yield _sse(
            "sources",
            {
                "sources": result.sources(),
                "considered": result.considered,
                "filtered_out": result.filtered_out,
                "lexical_only": result.lexical_only,
                "embeddings_pending": context.embeddings_pending,
                "engine": engine,
                "retrieval_ms": timing.retrieval_ms,
            },
        )

        if result.is_empty:
            message = no_context_message(language)
            yield _sse("token", {"text": message})
            yield _sse(
                "done",
                {
                    "answer": message,
                    "generated": False,
                    "engine": {"backend": "none", "model": None},
                    "timing": timing.snapshot(),
                },
            )
            return

        generated = False
        budget = context.settings.llm_timeout_seconds
        deadline = time.monotonic() + budget if budget > 0 else None
        try:
            if context.llm is not None and context.llm.available:
                messages = build_messages(
                    question, result.context, language=language, history=payload.history
                )
                stream = context.llm.stream(
                    messages,
                    temperature=context.settings.llm_temperature,
                    max_tokens=context.settings.llm_max_tokens,
                )
                try:
                    async for chunk in _sse_tokens(stream, answer_parts, timing, deadline):
                        yield chunk
                finally:
                    # Referme explicitement le moteur : son `finally` signale au fil
                    # llama.cpp d'arrêter, au lieu d'attendre le ramasse-miettes.
                    await stream.aclose()
                generated = True
                if timing.truncated:
                    yield _sse("notice", {"message": TRUNCATED_NOTICE})
            else:
                async for chunk in _sse_tokens(
                    stream_extractive_answer(question, result.hits, language=language),
                    answer_parts,
                    timing,
                ):
                    yield chunk
        except LLMUnavailable as exc:
            logger.warning("Moteur de génération indisponible, repli extractif : %s", exc)
            answer_parts.clear()
            engine = {"backend": "extractive", "model": None}
            yield _sse("notice", {"message": f"Génération indisponible ({exc}). Mode extractif."})
            async for chunk in _sse_tokens(
                stream_extractive_answer(question, result.hits, language=language),
                answer_parts,
                timing,
            ):
                yield chunk
        except asyncio.CancelledError:  # client déconnecté
            raise
        except Exception as exc:  # pragma: no cover - garde-fou
            logger.exception("Échec de la génération : %s", exc)
            yield _sse("error", {"message": "Erreur interne pendant la génération."})
            return

        answer = "".join(answer_parts)
        session.history = (session.history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ])[-8:]
        if not generated:
            engine = {"backend": "extractive", "model": None}
        stats = timing.snapshot()
        logger.info(
            "Réponse en %d ms (recherche %d ms, 1er jeton %d ms, %d jetons, %s)",
            stats["total_ms"],
            stats["retrieval_ms"],
            stats["first_token_ms"],
            stats["tokens"],
            engine.get("model") or engine.get("backend"),
        )
        yield _sse(
            "done",
            {"answer": answer, "generated": generated, "engine": engine, "timing": stats},
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/search")
async def search(
    q: str = Query(min_length=1, max_length=MAX_QUESTION_LENGTH),
    top_k: int = Query(default=10, ge=1, le=50),
    session: WebSession = Depends(require_session),
    context: AppContext = Depends(get_context),
) -> dict:
    """Recherche documentaire brute, sans génération."""
    result = await context.pipeline.retrieve(session.dsm, q, top_k=top_k)
    return {
        "query": q,
        "sources": result.sources(),
        "considered": result.considered,
        "filtered_out": result.filtered_out,
        "lexical_only": result.lexical_only,
        "embeddings_pending": context.embeddings_pending,
    }


@router.get("/documents")
async def documents(
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: WebSession = Depends(require_session),
    context: AppContext = Depends(get_context),
) -> dict:
    """Liste les documents indexés, restreinte aux partages visibles par l'utilisateur."""
    shares, home_prefix = await context.access.index_scope(session.dsm)
    items = await asyncio.to_thread(
        context.store.list_documents,
        limit=limit,
        offset=offset,
        query=q,
        shares=shares,
        home_prefix=home_prefix,
    )
    totals = await asyncio.to_thread(
        context.store.count_documents, query=q, shares=shares, home_prefix=home_prefix
    )
    return {"documents": items, "count": len(items), "total": totals}


@router.get("/document")
async def download_document(
    path: str = Query(min_length=1, max_length=4096),
    session: WebSession = Depends(require_session),
    context: AppContext = Depends(get_context),
) -> Response:
    """Télécharge un document **après** revalidation des droits de l'utilisateur."""
    if not await context.access.can_read(session.dsm, path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous n'avez pas accès à ce document.",
        )
    try:
        content = await context.client.download(session.dsm.sid, path)
    except DSMError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    filename = path.rsplit("/", 1)[-1]
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
