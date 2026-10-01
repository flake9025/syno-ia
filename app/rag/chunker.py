"""Découpage du texte en fragments adaptés à la récupération.

La stratégie est récursive : on privilégie les coupures « naturelles »
(paragraphes, puis phrases, puis mots) afin de ne jamais scinder une idée au
milieu d'un mot, tout en garantissant une taille maximale et un recouvrement
entre fragments consécutifs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WHITESPACE_RE = re.compile(r"[ \t\x0b\x0c\r]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÖØ-Þ0-9«\"'])")

#: Séparateurs essayés dans l'ordre, du plus « sémantique » au plus brutal.
SEPARATORS = ["\n\n", "\n", ". ", " "]


@dataclass
class TextSegment:
    """Portion de document accompagnée de sa localisation d'origine."""

    text: str
    location: str = ""


def clean_text(text: str) -> str:
    """Normalise les espaces sans détruire la structure en paragraphes."""
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    # Recolle les mots coupés en fin de ligne (fréquent dans les PDF).
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _split_recursive(text: str, max_size: int, depth: int = 0) -> list[str]:
    """Découpe `text` en morceaux d'au plus `max_size` caractères."""
    if len(text) <= max_size:
        return [text] if text.strip() else []
    if depth >= len(SEPARATORS):
        return [text[i : i + max_size] for i in range(0, len(text), max_size)]

    separator = SEPARATORS[depth]
    parts = text.split(separator)
    if len(parts) == 1:
        return _split_recursive(text, max_size, depth + 1)

    chunks: list[str] = []
    buffer = ""
    for part in parts:
        candidate = f"{buffer}{separator}{part}" if buffer else part
        if len(candidate) <= max_size:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        if len(part) > max_size:
            chunks.extend(_split_recursive(part, max_size, depth + 1))
            buffer = ""
        else:
            buffer = part
    if buffer.strip():
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk.strip()]


def _overlap_tail(text: str, overlap: int) -> str:
    """Extrait la fin de `text` à réutiliser en tête du fragment suivant."""
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    # On tente de repartir d'une frontière de phrase, sinon de mot.
    sentences = _SENTENCE_RE.split(tail)
    if len(sentences) > 1:
        return sentences[-1].strip()
    space = tail.find(" ")
    return tail[space + 1 :].strip() if space != -1 else tail.strip()


def chunk_segments(
    segments: list[TextSegment], *, chunk_size: int = 900, overlap: int = 150
) -> list[TextSegment]:
    """Transforme des segments bruts en fragments prêts à être indexés."""
    chunk_size = max(200, chunk_size)
    overlap = max(0, min(overlap, chunk_size // 2))

    results: list[TextSegment] = []
    for segment in segments:
        text = clean_text(segment.text)
        if not text:
            continue
        previous_tail = ""
        for piece in _split_recursive(text, chunk_size):
            body = f"{previous_tail}\n{piece}".strip() if previous_tail else piece
            if body.strip():
                results.append(TextSegment(text=body, location=segment.location))
            previous_tail = _overlap_tail(piece, overlap)
    return results


def chunk_text(text: str, *, chunk_size: int = 900, overlap: int = 150, location: str = "") -> list[TextSegment]:
    return chunk_segments([TextSegment(text=text, location=location)], chunk_size=chunk_size, overlap=overlap)
