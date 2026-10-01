"""Réponse extractive : mode dégradé sans LLM.

Quand aucun moteur de génération n'est disponible (NAS trop modeste, aucun
service distant configuré), l'application reste utile : elle sélectionne les
phrases des extraits qui recouvrent le mieux les termes de la question et les
restitue avec leurs citations. Aucune hallucination n'est possible puisque le
texte provient littéralement des documents.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence

from ..rag.store import SearchHit, tokenize_query

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")
#: Mots trop fréquents pour discriminer un passage.
_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "que", "qui",
    "quoi", "dans", "pour", "sur", "avec", "est", "sont", "au", "aux", "ce",
    "cette", "ces", "par", "pas", "plus", "son", "sa", "ses", "en", "il", "elle",
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "what", "which", "how", "why", "who",
}


def _keywords(question: str) -> set[str]:
    tokens = {token for token in tokenize_query(question) if token not in _STOPWORDS}
    return tokens or set(tokenize_query(question))


def _score_sentence(sentence: str, keywords: set[str]) -> float:
    tokens = set(tokenize_query(sentence))
    if not tokens or not keywords:
        return 0.0
    overlap = len(tokens & keywords)
    if overlap == 0:
        return 0.0
    # Favorise le recouvrement, pénalise légèrement les phrases très longues.
    return overlap / (1 + 0.002 * len(sentence))


def build_extractive_answer(
    question: str,
    hits: Sequence[SearchHit],
    *,
    language: str = "fr",
    max_sentences: int = 6,
) -> str:
    """Compose une réponse à partir des phrases les plus pertinentes des extraits."""
    if not hits:
        return ""
    keywords = _keywords(question)
    scored: list[tuple[float, int, str]] = []
    for index, hit in enumerate(hits, start=1):
        for sentence in _SENTENCE_SPLIT.split(hit.text):
            sentence = sentence.strip()
            if len(sentence) < 40:
                continue
            score = _score_sentence(sentence, keywords)
            if score > 0:
                scored.append((score, index, sentence))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[:max_sentences]
    if not selected:
        # Aucun recouvrement : on restitue le début du meilleur extrait.
        best = hits[0]
        snippet = best.text.strip()[:600]
        selected = [(0.0, 1, snippet)]

    # Rétablit l'ordre des documents pour une lecture naturelle.
    selected.sort(key=lambda item: item[1])

    if language.lower().startswith("fr"):
        header = (
            "Aucun modèle de langage n'est actif : voici les passages les plus "
            "pertinents trouvés dans vos documents.\n\n"
        )
    else:
        header = (
            "No language model is active: here are the most relevant passages found "
            "in your documents.\n\n"
        )
    body = "\n\n".join(f"[{index}] {sentence}" for _, index, sentence in selected)
    return header + body


async def stream_extractive_answer(
    question: str, hits: Sequence[SearchHit], *, language: str = "fr"
) -> AsyncIterator[str]:
    """Diffuse la réponse extractive par petits blocs, comme un vrai flux."""
    answer = build_extractive_answer(question, hits, language=language)
    for start in range(0, len(answer), 48):
        yield answer[start : start + 48]
