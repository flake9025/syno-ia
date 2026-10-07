"""Profilage matériel et sélection des modèles."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.hardware import (
    LLM_BY_PROFILE,
    LOCAL_LLM_RAM_MB,
    MINIMUM_RAM_MB,
    HardwareProfile,
    classify,
    compute_score,
    compute_tier,
    memory_tier,
)
from app.llm.factory import installed_model_path, local_model_path


def profile(cpu: int, ram: int, flags: list[str], total: int | None = None) -> HardwareProfile:
    score = compute_score(cpu, flags)
    return HardwareProfile(
        profile=classify(cpu, ram, flags),
        cpu_count=cpu,
        total_ram_mb=total if total is not None else ram,
        available_ram_mb=ram,
        architecture="x86_64",
        flags=flags,
        memory_tier=memory_tier(ram),
        compute_tier=compute_tier(score),
        compute_score=score,
    )


def test_ds218plus_dorigine_2go():
    """Celeron J3355, 2 cœurs sans AVX, 2 Go : tout est contraint."""
    hardware = profile(cpu=2, ram=1400, flags=["sse4_2"], total=2048)
    assert hardware.memory_tier == "micro"
    assert hardware.compute_tier == "micro"
    # Sans la moindre accélération vectorielle, on descend sous « micro ».
    assert hardware.profile == "nano"
    assert hardware.embedding_choice()[0] == "model2vec"


def test_ds218plus_etendu_8go():
    """Même CPU mais 8 Go : la mémoire ne rachète pas l'absence d'AVX."""
    hardware = profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192)
    assert hardware.memory_tier == "medium"
    assert hardware.compute_tier == "micro"
    # Aucun cran de tolérance sans SIMD : un modèle plus gros serait trois fois plus lent.
    assert hardware.profile == "nano"
    assert hardware.can_host_local_llm
    # Sans AVX2, l'inférence ONNX serait trop lente : on reste sur des embeddings statiques.
    assert hardware.embedding_choice()[0] == "model2vec"
    assert any("CPU limité" in warning for warning in hardware.warnings)


def test_nano_reserve_aux_processeurs_sans_simd():
    """Le moindre jeu d'instructions vectorielles suffit à mériter « micro »."""
    assert classify(2, 1400, ["sse4_2", "avx"]) == "micro"
    assert classify(2, 1400, ["asimd"]) == "micro"
    assert classify(2, 1400, []) == "nano"


def test_le_profil_nano_reste_coherent():
    hardware = profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192)
    choice = hardware.llm_choice()
    assert choice is not None and choice.approx_ram_mb < LLM_BY_PROFILE["micro"].approx_ram_mb
    # Les embeddings suivent la mémoire, pas le profil : l'index reste valide.
    assert hardware.embedding_profile in ("micro", "small", "medium", "large")
    assert hardware.estimated_tokens_per_second() > 0
    assert hardware.tuning()["retrieval_top_k"] > 0


def test_un_coeur_reste_libre_pour_le_serveur():
    """Sur un bicœur, saturer les deux cœurs faisait redémarrer le conteneur.

    Les fils de calcul de ggml tournent en attente active : sans cœur libre, la
    sonde de santé cessait de répondre et la réponse en cours était perdue.
    """
    assert profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192).recommended_threads() == 1
    assert profile(cpu=4, ram=6800, flags=["avx2"], total=8192).recommended_threads() == 3
    # Un monocœur ne peut rien réserver, mais ne doit jamais tomber à zéro.
    assert profile(cpu=1, ram=1200, flags=[], total=2048).recommended_threads() == 1


def test_le_prompt_suit_le_processeur_pas_la_memoire():
    """Un DS218+ a 8 Go mais un CPU lent : le prompt doit rester court.

    Le contexte est relu par le processeur avant le premier mot de la réponse.
    Le dimensionner sur la mémoire condamnait ces machines à plusieurs minutes
    d'attente, puis à une coupure réseau.
    """
    lent = profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192)
    rapide = profile(cpu=8, ram=6800, flags=["sse4_2", "avx", "avx2"], total=8192)

    assert lent.memory_tier == rapide.memory_tier  # même mémoire disponible
    assert lent.tuning()["context_max_chars"] < rapide.tuning()["context_max_chars"]
    assert lent.tuning()["context_max_chars"] <= 2000


def test_une_generation_deportee_desserre_le_prompt():
    """Brider le contexte n'a de sens que si c'est le NAS qui relit le prompt.

    Quand la rédaction part sur un PC, rationner les extraits appauvrirait la
    réponse pour épargner un processeur qui ne travaille plus.
    """
    lent = profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192)

    local = lent.tuning()
    deporte = lent.tuning(remote_generation=True)

    assert deporte["context_max_chars"] > local["context_max_chars"]
    assert deporte["history_turns"] > local["history_turns"]
    assert deporte["retrieval_top_k"] > local["retrieval_top_k"]
    # La recherche, elle, reste à la charge du NAS : inutile de l'alourdir.
    assert deporte["retrieval_candidates"] == local["retrieval_candidates"]


def test_une_machine_deja_genereuse_nest_pas_rabaissee():
    rapide = profile(cpu=8, ram=16000, flags=["sse4_2", "avx", "avx2"], total=16000)
    assert rapide.tuning(remote_generation=True) == rapide.tuning()


def test_la_tolerance_profite_aux_processeurs_accelerés():
    """Le même déséquilibre mémoire/CPU donne un cran de plus avec AVX."""
    hardware = profile(cpu=2, ram=6800, flags=["sse4_2", "avx"], total=8192)
    assert hardware.compute_tier == "micro"
    assert hardware.profile == "small"


def test_nas_avec_beaucoup_de_ram_et_avx2_utilise_onnx():
    hardware = profile(cpu=4, ram=7000, flags=["avx", "avx2"], total=8192)
    assert hardware.embedding_choice()[0] == "fastembed"


def test_nas_moderne_avx2():
    hardware = profile(cpu=8, ram=12000, flags=["avx", "avx2", "fma"], total=16384)
    assert hardware.compute_tier == "large"
    assert hardware.profile == "large"
    assert not hardware.recommends_remote_llm()


def test_la_memoire_plafonne_toujours_le_choix():
    """Beaucoup de cœurs mais peu de RAM : on ne charge pas un gros modèle."""
    hardware = profile(cpu=16, ram=1200, flags=["avx2"], total=2048)
    assert hardware.profile == "micro"
    assert hardware.recommends_remote_llm()


def test_seuil_minimal():
    assert not profile(cpu=2, ram=600, flags=[], total=700).meets_minimum
    assert profile(cpu=2, ram=1400, flags=[], total=2048).meets_minimum
    assert MINIMUM_RAM_MB < LOCAL_LLM_RAM_MB


def test_avx2_augmente_le_score():
    assert compute_score(4, ["avx2"]) > compute_score(4, ["sse4_2"])


def test_debit_estime_decroit_avec_la_taille_du_modele():
    faible = profile(cpu=2, ram=6800, flags=["sse4_2"])
    puissant = profile(cpu=8, ram=12000, flags=["avx2"])
    assert faible.estimated_tokens_per_second() > 0
    assert puissant.estimated_tokens_per_second() > 0


def test_serialisation_complete():
    data = profile(cpu=2, ram=6800, flags=["sse4_2"]).to_dict()
    for key in (
        "profile", "memory_tier", "compute_tier", "suggested_llm",
        "suggested_embedding", "warnings", "meets_minimum",
    ):
        assert key in data


# ------------------------------------------------- substitution de modèle
def _settings_avec_modeles(tmp_path: Path, *profils: str) -> Settings:
    settings = Settings(data_dir=tmp_path)
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    for nom in profils:
        (settings.models_dir / LLM_BY_PROFILE[nom].filename).touch()
    return settings


def test_modele_installe_substitue_celui_du_profil(tmp_path: Path):
    """Un modèle déjà téléchargé évite de retomber en extractif après un changement de profil."""
    settings = _settings_avec_modeles(tmp_path, "small")
    hardware = profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192)

    assert hardware.profile == "nano"
    attendu = local_model_path(settings, hardware)
    assert attendu is not None and attendu.name == LLM_BY_PROFILE["nano"].filename
    retenu = installed_model_path(settings, hardware)
    assert retenu is not None and retenu.name == LLM_BY_PROFILE["small"].filename


def test_le_modele_du_profil_prime_sur_les_autres(tmp_path: Path):
    settings = _settings_avec_modeles(tmp_path, "nano", "small")
    hardware = profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192)

    retenu = installed_model_path(settings, hardware)
    assert retenu is not None and retenu.name == LLM_BY_PROFILE["nano"].filename


def test_sans_modele_installe_le_chemin_attendu_est_conserve(tmp_path: Path):
    settings = _settings_avec_modeles(tmp_path)
    hardware = profile(cpu=2, ram=6800, flags=["sse4_2"], total=8192)

    retenu = installed_model_path(settings, hardware)
    assert retenu is not None and not retenu.exists()
    assert retenu.name == LLM_BY_PROFILE["nano"].filename
