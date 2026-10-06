"""Détection des capacités matérielles du NAS et sélection des modèles.

L'objectif est d'adapter automatiquement la taille des modèles (embeddings et
LLM) à la machine qui héberge le conteneur.

Deux dimensions sont évaluées **séparément**, car elles ne contraignent pas les
mêmes choses :

* la **mémoire** détermine ce qu'il est possible de charger ;
* le **calcul** (nombre de cœurs et jeux d'instructions SIMD) détermine ce qu'il
  est raisonnable d'exécuter en termes de latence.

Exemple concret : un DS218+ étendu à 8 Go dispose de largement assez de mémoire
pour un modèle 3B, mais son Celeron J3355 « Goldmont » n'a que 2 cœurs **et pas
d'AVX** — il produirait 1 à 2 jetons par seconde. Le profil retenu pour la
génération est donc le minimum des deux dimensions (avec un cran de tolérance
quand la mémoire est abondante), tandis que les embeddings, moins sensibles à la
latence, suivent principalement la mémoire disponible.
"""

from __future__ import annotations

import logging
import os
import platform
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_MAX = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_MEMINFO = Path("/proc/meminfo")
_CPUINFO = Path("/proc/cpuinfo")

PROFILE_ORDER = ["micro", "small", "medium", "large"]

#: Catalogue de modèles, du plus léger au plus lourd. « nano » n'est jamais le
#: résultat d'une mesure de mémoire ou de calcul : c'est le palier de repli pour
#: les processeurs dépourvus d'accélération vectorielle, où même 0.5B rame.
MODEL_ORDER = ["nano", *PROFILE_ORDER]

#: Socle minimal pour faire tourner syno-ia (en deçà, l'application démarre mais
#: alerte : l'indexation risque d'être interrompue par le tueur de mémoire).
MINIMUM_RAM_MB = 900
#: Confort d'utilisation (indexation + embeddings ONNX sans contention).
RECOMMENDED_RAM_MB = 4096
#: Mémoire à partir de laquelle un LLM local devient envisageable.
LOCAL_LLM_RAM_MB = 6144
#: Marge réservée à DSM, à l'indexation et au processus Python.
MEMORY_HEADROOM_MB = 700


@dataclass(frozen=True)
class ModelChoice:
    """Description d'un modèle GGUF téléchargeable depuis le Hub Hugging Face."""

    repo_id: str
    filename: str
    label: str
    approx_ram_mb: int

    @property
    def reference(self) -> str:
        return f"{self.repo_id}/{self.filename}"


#: Modèles LLM par profil, du plus léger au plus lourd.
LLM_BY_PROFILE: dict[str, ModelChoice] = {
    "nano": ModelChoice(
        repo_id="LiquidAI/LFM2-350M-GGUF",
        filename="LFM2-350M-Q4_K_M.gguf",
        label="LFM2 350M (Q4_K_M)",
        approx_ram_mb=330,
    ),
    "micro": ModelChoice(
        repo_id="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        label="Qwen2.5 0.5B Instruct (Q4_K_M)",
        approx_ram_mb=520,
    ),
    "small": ModelChoice(
        repo_id="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        label="Qwen2.5 1.5B Instruct (Q4_K_M)",
        approx_ram_mb=1250,
    ),
    "medium": ModelChoice(
        repo_id="Qwen/Qwen2.5-3B-Instruct-GGUF",
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        label="Qwen2.5 3B Instruct (Q4_K_M)",
        approx_ram_mb=2300,
    ),
    "large": ModelChoice(
        repo_id="Qwen/Qwen2.5-7B-Instruct-GGUF",
        filename="qwen2.5-7b-instruct-q4_k_m.gguf",
        label="Qwen2.5 7B Instruct (Q4_K_M)",
        approx_ram_mb=5000,
    ),
}

#: Modèles d'embeddings par profil.
#: - model2vec : embeddings statiques (pur NumPy), très rapides sur CPU faible.
#: - fastembed : ONNX, meilleure qualité, plus gourmand en RAM et en CPU.
EMBEDDING_BY_PROFILE: dict[str, tuple[str, str]] = {
    "micro": ("model2vec", "minishlab/potion-multilingual-128M"),
    "small": ("model2vec", "minishlab/potion-multilingual-128M"),
    "medium": ("fastembed", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
    "large": ("fastembed", "intfloat/multilingual-e5-large"),
}

#: Paramètres de récupération ajustés au profil.
TUNING_BY_PROFILE: dict[str, dict[str, int]] = {
    "micro": {"retrieval_candidates": 40, "retrieval_top_k": 4, "context_max_chars": 3500},
    "small": {"retrieval_candidates": 60, "retrieval_top_k": 5, "context_max_chars": 5000},
    "medium": {"retrieval_candidates": 80, "retrieval_top_k": 6, "context_max_chars": 7000},
    "large": {"retrieval_candidates": 120, "retrieval_top_k": 8, "context_max_chars": 10000},
}

_INTERESTING_FLAGS = {"avx", "avx2", "avx512f", "fma", "f16c", "sse4_2", "neon", "asimd"}


@dataclass
class HardwareProfile:
    profile: str
    cpu_count: int
    total_ram_mb: int
    available_ram_mb: int
    architecture: str
    cpu_model: str = ""
    flags: list[str] = field(default_factory=list)
    detected_from: str = "auto"
    memory_tier: str = "micro"
    compute_tier: str = "micro"
    compute_score: float = 0.0

    @property
    def has_avx(self) -> bool:
        return "avx" in self.flags

    @property
    def has_avx2(self) -> bool:
        return "avx2" in self.flags

    @property
    def is_arm(self) -> bool:
        return self.architecture.startswith(("aarch64", "arm"))

    @property
    def meets_minimum(self) -> bool:
        return self.total_ram_mb >= MINIMUM_RAM_MB

    @property
    def can_host_local_llm(self) -> bool:
        return self.total_ram_mb >= LOCAL_LLM_RAM_MB

    @property
    def warnings(self) -> list[str]:
        issues: list[str] = []
        if not self.meets_minimum:
            issues.append(
                f"RAM insuffisante ({self.total_ram_mb} Mo) : {MINIMUM_RAM_MB} Mo au minimum, "
                f"{RECOMMENDED_RAM_MB} Mo recommandés."
            )
        elif self.total_ram_mb < RECOMMENDED_RAM_MB:
            issues.append(
                f"RAM limitée ({self.total_ram_mb} Mo) : indexation plus lente, "
                f"{RECOMMENDED_RAM_MB} Mo recommandés."
            )
        if self.compute_tier == "micro" and self.memory_tier in ("medium", "large"):
            issues.append(
                "Mémoire confortable mais CPU limité (peu de cœurs ou pas d'AVX) : "
                "un LLM local restera lent, un serveur Ollama distant est conseillé."
            )
        return issues

    @property
    def embedding_profile(self) -> str:
        """Les embeddings suivent la mémoire, avec un cran de tolérance sur le CPU."""
        memory_index = PROFILE_ORDER.index(self.memory_tier)
        compute_index = PROFILE_ORDER.index(self.compute_tier)
        return PROFILE_ORDER[min(memory_index, compute_index + 1)]

    def llm_choice(self) -> ModelChoice | None:
        return LLM_BY_PROFILE.get(self.profile)

    def embedding_choice(self) -> tuple[str, str]:
        return EMBEDDING_BY_PROFILE.get(self.embedding_profile, EMBEDDING_BY_PROFILE["micro"])

    def tuning(self) -> dict[str, int]:
        return TUNING_BY_PROFILE.get(self.memory_tier, TUNING_BY_PROFILE["micro"])

    def recommended_threads(self) -> int:
        """Laisse au moins un cœur à DSM dès que la machine en possède plus de deux."""
        return max(1, self.cpu_count - 1) if self.cpu_count > 2 else max(1, self.cpu_count)

    def estimated_tokens_per_second(self) -> float:
        """Estimation grossière du débit de génération d'un modèle Q4 local."""
        choice = self.llm_choice()
        if choice is None:
            return 0.0
        billions = {"nano": 0.35, "micro": 0.5, "small": 1.5, "medium": 3.0, "large": 7.0}[
            self.profile
        ]
        simd = 2.2 if self.has_avx2 else (1.4 if self.has_avx else 1.0)
        return round(max(0.2, (5.5 * self.cpu_count * simd) / billions), 1)

    def recommends_remote_llm(self) -> bool:
        """Un LLM local est-il déconseillé sur cette machine ?"""
        choice = self.llm_choice()
        if choice is None:
            return True
        if self.available_ram_mb < choice.approx_ram_mb + MEMORY_HEADROOM_MB:
            return True
        # En dessous de ~3 jetons/s, l'expérience devient pénible.
        return self.estimated_tokens_per_second() < 3.0

    def to_dict(self) -> dict:
        data = asdict(self)
        choice = self.llm_choice()
        data.update(
            {
                "has_avx": self.has_avx,
                "has_avx2": self.has_avx2,
                "is_arm": self.is_arm,
                "meets_minimum": self.meets_minimum,
                "can_host_local_llm": self.can_host_local_llm,
                "recommends_remote_llm": self.recommends_remote_llm(),
                "recommended_threads": self.recommended_threads(),
                "estimated_tokens_per_second": self.estimated_tokens_per_second(),
                "embedding_profile": self.embedding_profile,
                "suggested_llm": choice.label if choice else None,
                "suggested_embedding": self.embedding_choice()[1],
                "warnings": self.warnings,
            }
        )
        return data


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _meminfo_kb(key: str) -> int | None:
    try:
        content = _MEMINFO.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf"^{re.escape(key)}:\s+(\d+) kB", content, re.MULTILINE)
    return int(match.group(1)) if match else None


def _cpu_info() -> tuple[str, list[str]]:
    try:
        content = _CPUINFO.read_text(encoding="utf-8")
    except OSError:
        return platform.processor() or platform.machine(), []
    model = ""
    flags: list[str] = []
    for line in content.splitlines():
        lowered = line.lower()
        if not model and lowered.startswith(("model name", "hardware")):
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[1].strip():
                model = parts[1].strip()
        if not flags and lowered.startswith(("flags", "features")):
            flags = line.split(":", 1)[1].split()
    return model or platform.machine(), [f.lower() for f in flags]


def _memory_mb() -> tuple[int, int]:
    """Retourne (RAM totale visible, RAM disponible) en Mo, limites cgroup incluses."""
    total_kb = _meminfo_kb("MemTotal")
    available_kb = _meminfo_kb("MemAvailable") or total_kb

    total_mb = int(total_kb / 1024) if total_kb else 0
    available_mb = int(available_kb / 1024) if available_kb else 0

    # Une limite cgroup (docker run -m) prime sur la RAM physique.
    for cgroup_file in (_CGROUP_V2_MAX, _CGROUP_V1_MAX):
        limit = _read_int(cgroup_file)
        # Les valeurs « illimité » de cgroup v1 sont d'énormes entiers.
        if limit and limit < (1 << 62):
            limit_mb = int(limit / (1024 * 1024))
            if total_mb == 0 or limit_mb < total_mb:
                total_mb = limit_mb
                available_mb = min(available_mb or limit_mb, limit_mb)

    if total_mb == 0:  # environnement non Linux (tests sous Windows/macOS)
        total_mb, available_mb = 4096, 2048
    return total_mb, available_mb or total_mb


def memory_tier(available_ram_mb: int) -> str:
    """Ce que la mémoire disponible permet de charger."""
    if available_ram_mb < 1600:
        return "micro"
    if available_ram_mb < 3500:
        return "small"
    if available_ram_mb < 7000:
        return "medium"
    return "large"


def compute_score(cpu_count: int, flags: list[str]) -> float:
    """Indice de puissance CPU : cœurs pondérés par les jeux d'instructions SIMD.

    Repères : DS218+ (2 cœurs Goldmont sans AVX) ≈ 2,0 ; DS923+ (Ryzen R1600,
    4 threads AVX2) ≈ 6,4 ; DS1621+ (Ryzen V1500B, 8 threads AVX2) ≈ 12,8.
    """
    if "avx2" in flags or "avx512f" in flags:
        multiplier = 1.6
    elif "avx" in flags:
        multiplier = 1.15
    elif "asimd" in flags or "neon" in flags:
        multiplier = 1.1
    else:
        multiplier = 1.0
    return round(cpu_count * multiplier, 2)


def compute_tier(score: float) -> str:
    if score < 2.5:
        return "micro"
    if score < 5.0:
        return "small"
    if score < 9.0:
        return "medium"
    return "large"


def classify(cpu_count: int, available_ram_mb: int, flags: list[str] | None = None) -> str:
    """Profil retenu pour la **génération** : le calcul prime, la mémoire plafonne.

    Un cran de tolérance est accordé lorsque la mémoire est abondante, mais
    uniquement si le processeur dispose d'une accélération SIMD. La mémoire ne
    compense pas un processeur lent : sur un DS218+ sans AVX, un modèle plus gros
    ne ferait qu'allonger l'attente, chaque jeton coûtant trois fois plus cher.

    Sans aucune accélération vectorielle, même 0.5B reste pénible : on descend
    alors au palier « nano », dont le modèle tient en 350 millions de paramètres.
    """
    flags = flags or []
    memory = memory_tier(available_ram_mb)
    compute = compute_tier(compute_score(cpu_count, flags))
    memory_index = PROFILE_ORDER.index(memory)
    compute_index = PROFILE_ORDER.index(compute)
    accelerated = any(flag in flags for flag in ("avx", "avx2", "avx512f", "asimd", "neon"))
    tolerance = 1 if memory_index > compute_index and accelerated else 0
    profile = PROFILE_ORDER[min(memory_index, compute_index + tolerance)]
    return "nano" if profile == "micro" and not accelerated else profile


def detect_hardware(forced_profile: str = "auto") -> HardwareProfile:
    """Analyse la machine hôte et retourne le profil matériel retenu."""
    cpu_count = os.cpu_count() or 1
    total_mb, available_mb = _memory_mb()
    cpu_model, all_flags = _cpu_info()
    flags = [flag for flag in all_flags if flag in _INTERESTING_FLAGS]

    score = compute_score(cpu_count, flags)
    profile = classify(cpu_count, available_mb, flags)
    detected_from = "auto"
    if forced_profile and forced_profile != "auto":
        if forced_profile in MODEL_ORDER:
            profile, detected_from = forced_profile, "forced"
        else:
            logger.warning(
                "Profil matériel inconnu « %s », détection automatique utilisée", forced_profile
            )

    hardware = HardwareProfile(
        profile=profile,
        cpu_count=cpu_count,
        total_ram_mb=total_mb,
        available_ram_mb=available_mb,
        architecture=platform.machine(),
        cpu_model=cpu_model,
        flags=flags,
        detected_from=detected_from,
        memory_tier=memory_tier(available_mb),
        compute_tier=compute_tier(score),
        compute_score=score,
    )
    logger.info(
        "Profil matériel : %s (mémoire=%s, calcul=%s, %d cœurs, %d Mo dispo / %d Mo, "
        "arch=%s, avx2=%s, ~%.1f jetons/s)",
        hardware.profile,
        hardware.memory_tier,
        hardware.compute_tier,
        hardware.cpu_count,
        hardware.available_ram_mb,
        hardware.total_ram_mb,
        hardware.architecture,
        hardware.has_avx2,
        hardware.estimated_tokens_per_second(),
    )
    for warning in hardware.warnings:
        logger.warning("Matériel : %s", warning)
    return hardware
