"""Interrogation des documents : recherche, génération en flux et téléchargement."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

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


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    history: list[dict] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=20)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    session: WebSession = Depends(require_session),
    context: AppContext = Depends(get_context),
) -> StreamingResponse:
    """Répond à une question en flux SSE : `sources`, puis `token`, puis `done`."""
    question = payload.question.strip()
    language = session.language or context.settings.default_language

    async def event_stream() -> AsyncIterator[str]:
        answer_parts: list[str] = []
        try:
            result = await context.pipeline.retrieve(
                session.dsm, question, top_k=payload.top_k
            )
        except DSMError as exc:
            yield _sse("error", {"message": f"DSM : {exc.message}", "code": exc.code})
            return
        except Exception as exc:  # pragma: no cover - garde-fou
            logger.exception("Échec de la récupération : %s", exc)
            yield _sse("error", {"message": "Erreur interne pendant la recherche."})
            return

        yield _sse(
            "sources",
            {
                "sources": result.sources(),
                "considered": result.considered,
                "filtered_out": result.filtered_out,
                "lexical_only": result.lexical_only,
                "embeddings_pending": context.embeddings_pending,
            },
        )

        if result.is_empty:
            message = no_context_message(language)
            yield _sse("token", {"text": message})
            yield _sse("done", {"answer": message, "generated": False})
            return

        generated = False
        try:
            if context.llm is not None and context.llm.available:
                messages = build_messages(
                    question, result.context, language=language, history=payload.history
                )
                async for token in context.llm.stream(
                    messages,
                    temperature=context.settings.llm_temperature,
                    max_tokens=context.settings.llm_max_tokens,
                ):
                    answer_parts.append(token)
                    yield _sse("token", {"text": token})
                generated = True
            else:
                async for token in stream_extractive_answer(
                    question, result.hits, language=language
                ):
                    answer_parts.append(token)
                    yield _sse("token", {"text": token})
        except LLMUnavailable as exc:
            logger.warning("Moteur de génération indisponible, repli extractif : %s", exc)
            answer_parts.clear()
            yield _sse("notice", {"message": f"Génération indisponible ({exc}). Mode extractif."})
            async for token in stream_extractive_answer(question, result.hits, language=language):
                answer_parts.append(token)
                yield _sse("token", {"text": token})
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
        yield _sse("done", {"answer": answer, "generated": generated})

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
