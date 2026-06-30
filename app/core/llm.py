"""Accès aux modèles Ollama : embeddings dense (bge-m3) et génération.

100 % local : appels HTTP vers le service Ollama interne. Aucune sortie réseau
externe. La génération en streaming (SSE) sera ajoutée à la brique génération ;
ce module fournit déjà l'embedding dense par lots, utilisé à l'indexation et à
la recherche.
"""
from __future__ import annotations

import httpx

from core.config import settings

EMBED_BATCH = 32  # lots d'embeddings (compromis débit / mémoire)


def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Calcule les embeddings denses d'une liste de textes, par lots de 32.

    Utilise l'endpoint /api/embed d'Ollama (entrée par lot). Renvoie une liste
    de vecteurs (1024 dim pour bge-m3), dans l'ordre des textes fournis.
    """
    model = model or settings.EMBED_MODEL
    out: list[list[float]] = []
    with httpx.Client(timeout=120.0) as client:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            r = client.post(f"{settings.OLLAMA_URL}/api/embed",
                            json={"model": model, "input": batch})
            r.raise_for_status()
            out.extend(r.json()["embeddings"])
    return out


def embed_query(text: str, model: str | None = None) -> list[float]:
    """Embedding dense d'une requête unique."""
    return embed_texts([text], model=model)[0]
