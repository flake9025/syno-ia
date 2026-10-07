"""Construction des invites envoyées au modèle."""

from __future__ import annotations

SYSTEM_PROMPT_FR = """Tu es l'assistant documentaire privé d'un NAS Synology.
Tu réponds uniquement à partir des EXTRAITS fournis, qui proviennent de documents
auxquels l'utilisateur a explicitement le droit d'accéder.

Règles impératives :
- Ne t'appuie que sur les extraits ; n'invente jamais de contenu.
- Cite tes sources avec les marqueurs [1], [2]… correspondant aux extraits utilisés.
- Si les extraits ne permettent pas de répondre, dis-le clairement et propose une
  reformulation de la question.
- Réponds dans la langue de la question, de façon concise et structurée.
- Ne mentionne jamais l'existence de documents absents des extraits."""

SYSTEM_PROMPT_EN = """You are the private document assistant of a Synology NAS.
Answer strictly from the provided EXCERPTS, which come from documents the user is
explicitly allowed to access.

Rules:
- Rely only on the excerpts; never invent content.
- Cite sources using the [1], [2]… markers of the excerpts you used.
- If the excerpts are insufficient, say so clearly and suggest a rephrasing.
- Answer in the language of the question, concisely and in a structured way.
- Never mention documents that are not in the excerpts."""

#: Consignes abrégées pour les très petits modèles.
#:
#: Chaque jeton d'invite doit être relu par le processeur avant le premier mot de
#: la réponse : sur un NAS modeste, le préambule complet coûte à lui seul des
#: dizaines de secondes. Un modèle de 350 M de paramètres suit de toute façon
#: mieux trois consignes brèves qu'une liste de six.
SYSTEM_PROMPT_COMPACT_FR = """Réponds uniquement à partir des EXTRAITS fournis.
N'invente rien. Cite tes sources avec [1], [2].
Si les extraits ne suffisent pas, dis-le."""

SYSTEM_PROMPT_COMPACT_EN = """Answer only from the provided EXCERPTS.
Invent nothing. Cite sources as [1], [2].
If the excerpts are insufficient, say so."""

NO_CONTEXT_FR = (
    "Aucun document accessible ne correspond à cette question. "
    "Vérifiez l'orthographe, essayez d'autres mots-clés, ou contactez "
    "l'administrateur si vous pensez devoir avoir accès à ces documents."
)
NO_CONTEXT_EN = (
    "No accessible document matches this question. Check the spelling, try other "
    "keywords, or contact your administrator if you believe you should have access."
)


def system_prompt(language: str = "fr", *, compact: bool = False) -> str:
    francais = language.lower().startswith("fr")
    if compact:
        return SYSTEM_PROMPT_COMPACT_FR if francais else SYSTEM_PROMPT_COMPACT_EN
    return SYSTEM_PROMPT_FR if francais else SYSTEM_PROMPT_EN


def no_context_message(language: str = "fr") -> str:
    return NO_CONTEXT_FR if language.lower().startswith("fr") else NO_CONTEXT_EN


def build_messages(
    question: str,
    context: str,
    *,
    language: str = "fr",
    history: list[dict] | None = None,
    max_history: int = 4,
    compact: bool = False,
) -> list[dict]:
    """Assemble la conversation : système, historique récent, puis extraits + question.

    `max_history` et `compact` servent le même objectif : limiter le nombre de
    jetons que le modèle doit relire avant d'écrire. Sur un petit processeur, ce
    volume se paie directement en secondes d'attente.
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt(language, compact=compact)}]

    # Un tour d'historique trop long coûterait plus cher que les extraits eux-mêmes.
    taille_tour = 400 if compact else 2000
    for entry in (history or [])[-max_history:] if max_history > 0 else []:
        role = entry.get("role")
        content = (entry.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:taille_tour]})

    if language.lower().startswith("fr"):
        user_block = (
            f"EXTRAITS DISPONIBLES :\n\n{context}\n\n"
            f"QUESTION : {question}\n\n"
            "Réponds en citant les extraits utilisés sous la forme [1], [2]."
        )
    else:
        user_block = (
            f"AVAILABLE EXCERPTS:\n\n{context}\n\n"
            f"QUESTION: {question}\n\n"
            "Answer and cite the excerpts you used as [1], [2]."
        )
    messages.append({"role": "user", "content": user_block})
    return messages
