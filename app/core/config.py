"""Configuration centrale d'EDGAR v2.

Charge les paramètres depuis l'environnement (injecté par docker-compose / .env).
Aucune valeur sensible en dur ; les chemins pointent vers les volumes persistants.
"""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    """Paramètres applicatifs résolus au démarrage."""

    VERSION: str = "1.3.2"

    # --- Services internes ---
    OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    QDRANT_URL: str = os.environ.get("QDRANT_URL", "http://qdrant:6333")

    # --- Modèles ---
    EMBED_MODEL: str = os.environ.get("EMBED_MODEL", "bge-m3")  # dense, imposé
    LLM_MODEL: str = os.environ.get("LLM_MODEL", "mistral:7b")  # génération par défaut

    # --- Sécurité ---
    SECRET_KEY: str = os.environ.get("EDGAR_SECRET_KEY", "dev-insecure-secret")
    TLS: bool = os.environ.get("EDGAR_TLS", "0") == "1"

    # --- Chemins (volumes Docker) ---
    BASE_DIR: Path = Path(__file__).resolve().parent.parent  # /app
    DATA_DIR: Path = Path(os.environ.get("EDGAR_DATA_DIR", "/app/data"))
    DOCUMENTS_DIR: Path = Path(os.environ.get("EDGAR_DOCS_DIR", "/app/documents"))
    STATIC_DIR: Path = BASE_DIR / "static"
    TEMPLATES_DIR: Path = BASE_DIR / "templates"
    # Modèles bundlés dans l'image (FastEmbed BM25, reranker…), offline.
    MODELS_DIR: Path = Path(os.environ.get("EDGAR_MODELS_DIR", str(BASE_DIR / "models")))

    # --- Fichiers de configuration ---
    @property
    def db_path(self) -> Path:
        return self.DATA_DIR / "edgar2.db"

    @property
    def bases_json(self) -> Path:
        return self.DATA_DIR / "bases.json"

    @property
    def settings_json(self) -> Path:
        return self.DATA_DIR / "settings.json"

    def ensure_dirs(self) -> None:
        """Crée les dossiers de données s'ils n'existent pas (volumes montés vides)."""
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
