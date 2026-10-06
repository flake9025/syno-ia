"""Sélection automatique du moteur de génération selon la configuration et le matériel.

Ordre de préférence en mode « auto » :

1. une API compatible OpenAI si une clé est fournie ;
2. un serveur Ollama joignable (idéal : un PC du réseau, bien plus puissant que le NAS) ;
3. llama.cpp en local **si** le profil matériel le supporte et que le modèle est présent ;
4. aucun moteur → réponses extractives (voir `extractive.py`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from ..hardware import LLM_BY_PROFILE, MODEL_ORDER, HardwareProfile
from .base import LLMBackend
from .llamacpp import LlamaCppBackend, llama_cpp_available
from .ollama import OllamaBackend
from .openai_compat import OpenAICompatibleBackend

logger = logging.getLogger(__name__)


def local_model_path(settings: Settings, hardware: HardwareProfile) -> Path | None:
    """Chemin attendu du modèle GGUF local pour ce profil matériel."""
    if settings.llm_model and settings.llm_model.endswith(".gguf"):
        candidate = Path(settings.llm_model)
        return candidate if candidate.is_absolute() else settings.models_dir / settings.llm_model
    choice = hardware.llm_choice()
    return settings.models_dir / choice.filename if choice else None


def installed_model_path(
    settings: Settings, hardware: HardwareProfile, *, quiet: bool = False
) -> Path | None:
    """Modèle réellement présent le mieux adapté, à défaut de celui attendu.

    Le profil matériel peut changer d'une version à l'autre, ou après ajout de
    mémoire. Plutôt que de retomber en mode extractif alors qu'un modèle utilisable
    est déjà téléchargé, on retient le plus gros modèle installé qui ne dépasse pas
    le profil ; si tous le dépassent, le plus petit d'entre eux.
    """
    expected = local_model_path(settings, hardware)
    if expected is not None and expected.exists():
        return expected
    if settings.llm_model and settings.llm_model.endswith(".gguf"):
        return expected  # chemin imposé par l'exploitant : pas de substitution

    limit = MODEL_ORDER.index(hardware.profile)
    installed = [
        (MODEL_ORDER.index(name), settings.models_dir / choice.filename)
        for name, choice in LLM_BY_PROFILE.items()
        if (settings.models_dir / choice.filename).exists()
    ]
    if not installed:
        return expected
    affordable = [item for item in installed if item[0] <= limit]
    rank, path = max(affordable) if affordable else min(installed)
    if not quiet:
        logger.warning(
            "Modèle du profil « %s » absent : utilisation de %s (profil « %s ») déjà installé",
            hardware.profile,
            path.name,
            MODEL_ORDER[rank],
        )
    return path


async def build_llm(settings: Settings, hardware: HardwareProfile) -> LLMBackend | None:
    """Instancie le moteur de génération, ou None pour le mode extractif."""
    backend = (settings.llm_backend or "auto").lower()

    if backend == "none":
        logger.info("Génération désactivée : mode extractif (citations brutes)")
        return None

    if backend == "openai":
        return OpenAICompatibleBackend(
            settings.openai_base_url, settings.openai_api_key, settings.llm_model or "gpt-4o-mini"
        )

    if backend == "ollama":
        return OllamaBackend(settings.ollama_url, settings.llm_model or "qwen2.5:1.5b-instruct")

    if backend == "llamacpp":
        return _build_llamacpp(settings, hardware)

    # ------------------------------------------------------------------ auto
    if settings.openai_api_key:
        llm = OpenAICompatibleBackend(
            settings.openai_base_url, settings.openai_api_key, settings.llm_model or "gpt-4o-mini"
        )
        logger.info("Moteur retenu : API compatible OpenAI (%s)", llm.model)
        return llm

    if settings.ollama_url:
        candidate = OllamaBackend(settings.ollama_url, settings.llm_model or "")
        if await candidate.health():
            models = await candidate.list_models()
            if settings.llm_model and settings.llm_model in models:
                candidate.model = settings.llm_model
            elif models:
                candidate.model = _pick_ollama_model(models)
            if candidate.model:
                logger.info("Moteur retenu : Ollama %s (%s)", candidate.model, settings.ollama_url)
                return candidate
            logger.warning("Ollama est joignable mais aucun modèle n'est installé")
        await candidate.aclose()

    local = _build_llamacpp(settings, hardware, silent=True)
    if local is not None:
        logger.info("Moteur retenu : llama.cpp local (%s)", local.model)
        return local

    logger.warning(
        "Aucun moteur de génération disponible : les réponses seront extractives. "
        "Configurez OLLAMA_URL ou OPENAI_API_KEY, ou téléchargez un modèle local."
    )
    return None


def _pick_ollama_model(models: list[str]) -> str:
    """Privilégie un modèle instruct léger parmi ceux installés."""
    preferred = ("qwen2.5", "llama3.2", "phi3", "mistral", "gemma2")
    for marker in preferred:
        for model in models:
            if marker in model.lower() and "embed" not in model.lower():
                return model
    non_embedding = [model for model in models if "embed" not in model.lower()]
    return non_embedding[0] if non_embedding else ""


def _build_llamacpp(
    settings: Settings, hardware: HardwareProfile, *, silent: bool = False
) -> LlamaCppBackend | None:
    if not llama_cpp_available():
        if not silent:
            logger.warning("llama-cpp-python absent de l'image : moteur local indisponible")
        return None
    path = installed_model_path(settings, hardware, quiet=silent)
    if path is None or not path.exists():
        if not silent:
            logger.warning("Modèle GGUF absent (%s) : téléchargez-le depuis l'administration", path)
        return None
    if hardware.recommends_remote_llm() and not silent:
        logger.warning(
            "La RAM disponible (%d Mo) est juste pour ce modèle : privilégiez un serveur distant",
            hardware.available_ram_mb,
        )
    labels = {choice.filename: choice.label for choice in LLM_BY_PROFILE.values()}
    return LlamaCppBackend(
        path,
        context_size=settings.llm_context_size,
        threads=settings.llm_threads or hardware.recommended_threads(),
        model_label=labels.get(path.name, path.name),
    )


def download_model(repo_id: str, filename: str, target_dir: Path) -> Path:
    """Télécharge un modèle GGUF depuis le Hub Hugging Face (appel bloquant)."""
    from huggingface_hub import hf_hub_download

    target_dir.mkdir(parents=True, exist_ok=True)
    # `local_dir_use_symlinks` est supprimé dans huggingface_hub 1.x : depuis la
    # 0.23, `local_dir` écrit déjà les fichiers réels, sans lien symbolique.
    downloaded = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_dir=str(target_dir),
    )
    return Path(downloaded)
