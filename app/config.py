"""Configuration de l'application, entièrement pilotable par variables d'environnement."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Les listes se saisissent en clair (« a,b,c ») dans .env et docker-compose.
#: `NoDecode` empêche pydantic-settings de tenter un `json.loads` au préalable.
CsvList = Annotated[list[str], NoDecode]


def _split_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "syno-ia"
    app_version: str = "1.0.0"
    build_sha: str = "dev"
    build_date: str = ""
    port: int = 8080
    log_level: str = "INFO"
    default_language: str = "fr"

    #: Clé de signature des jetons de session. Générée aléatoirement si absente
    #: (les sessions sont alors invalidées à chaque redémarrage).
    app_secret: str = Field(default_factory=lambda: os.urandom(32).hex())

    #: Répertoire persistant (index SQLite, modèles téléchargés).
    data_dir: Path = Path("/app/data")

    # ------------------------------------------------------------- synology
    #: URL de l'interface DSM joignable depuis le conteneur. Depuis un conteneur
    #: tournant sur le NAS, la passerelle du bridge (172.17.0.1) pointe vers le NAS.
    dsm_url: str = "http://172.17.0.1:5000"
    dsm_verify_ssl: bool = False
    dsm_timeout: float = 20.0
    #: Nom de session DSM ; « FileStation » est requis pour SYNO.FileStation.*
    dsm_session_name: str = "FileStation"

    #: Compte de service (administrateur ou compte disposant d'un accès lecture
    #: aux dossiers à indexer). Utilisé uniquement pour l'indexation et la
    #: résolution des chemins réels des partages — jamais pour répondre à un
    #: utilisateur.
    dsm_service_account: str = ""
    dsm_service_password: str = ""

    #: Comptes DSM autorisés à administrer syno-ia en plus des administrateurs
    #: DSM (détectés via SYNO.FileStation.Info → is_manager).
    admin_accounts: CsvList = Field(default_factory=list)

    #: Durée de vie d'une session web (minutes).
    session_ttl_minutes: int = 720
    #: Durée de mise en cache d'une décision d'autorisation (secondes).
    acl_cache_ttl: int = 300
    #: Vérification fichier par fichier via FileStation en plus du filtre par
    #: partage. À ne désactiver que si aucune ACL avancée n'est utilisée.
    acl_strict: bool = True

    # ------------------------------------------------------------ indexation
    #: Mode de lecture des documents :
    #:   - « mount »       : partages montés dans le conteneur (rapide).
    #:   - « filestation » : téléchargement via l'API FileStation (aucun montage).
    index_mode: str = "mount"
    #: Racines à indexer. En mode « mount », chemins réels du NAS montés à
    #: l'identique dans le conteneur (ex. /volume1/documents).
    index_roots: CsvList = Field(default_factory=lambda: ["/volume1/documents"])
    index_exclude_globs: CsvList = Field(
        default_factory=lambda: [
            "**/@eaDir/**",
            "**/#recycle/**",
            "**/.*/**",
            "**/node_modules/**",
            "**/*.tmp",
        ]
    )
    index_max_file_mb: float = 40.0
    #: Intervalle de réindexation automatique (minutes ; 0 = désactivé).
    index_interval_minutes: int = 360
    index_on_startup: bool = True
    #: Nombre de fichiers traités entre deux commits SQLite.
    index_batch_size: int = 32

    chunk_size: int = 900
    chunk_overlap: int = 150

    # -------------------------------------------------------------- retrieval
    #: 0 = valeur déduite du profil matériel (voir hardware.TUNING_BY_PROFILE).
    retrieval_candidates: int = 0
    retrieval_top_k: int = 0
    context_max_chars: int = 0

    # ------------------------------------------------------------- embeddings
    #: « auto » | « model2vec » | « fastembed » | « none »
    embedding_backend: str = "auto"
    embedding_model: str = ""
    #: Charge le modèle en arrière-plan : l'application répond immédiatement en
    #: recherche lexicale (BM25), puis bascule en hybride une fois le moteur
    #: prêt. Passer à `false` rend le démarrage bloquant — le premier
    #: téléchargement peut alors dépasser le délai du healthcheck Docker.
    embedding_async_load: bool = True
    #: Nombre de fragments vectorisés par lot lors du rattrapage.
    embedding_backfill_batch: int = 64

    # -------------------------------------------------------------------- llm
    #: « auto » | « llamacpp » | « ollama » | « openai » | « none »
    llm_backend: str = "auto"
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 700
    llm_context_size: int = 4096
    llm_threads: int = 0

    ollama_url: str = "http://172.17.0.1:11434"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""

    #: Forçage manuel du profil matériel (« micro », « small », « medium », « large »).
    hardware_profile: str = "auto"

    # ---------------------------------------------------------------- setters
    @field_validator("admin_accounts", "index_roots", "index_exclude_globs", mode="before")
    @classmethod
    def _as_list(cls, value: object) -> list[str]:
        return _split_list(value)  # type: ignore[arg-type]

    @field_validator("dsm_url", "ollama_url", "openai_base_url", mode="before")
    @classmethod
    def _strip_slash(cls, value: object) -> object:
        return str(value).rstrip("/") if isinstance(value, str) else value

    # --------------------------------------------------------------- dérivés
    @property
    def db_path(self) -> Path:
        return self.data_dir / "syno-ia.db"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def has_service_account(self) -> bool:
        return bool(self.dsm_service_account and self.dsm_service_password)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
