"""Reranking par cross-encoder `bge-reranker-v2-m3`, exécuté sur **CPU**.

Le GPU reste réservé à Ollama ; le reranker tourne sur CPU dans le conteneur app.
Les poids sont bundlés dans l'image (offline). Le score est ramené dans [0,1]
par une sigmoïde, ce qui rend le **seuil de pertinence** interprétable.
"""
from __future__ import annotations

from typing import Any, Optional

_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
_model: Optional[Any] = None


def _get_model():
    """Charge le cross-encoder (singleton paresseux), depuis le cache bundlé, sur CPU."""
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder
        # Le cache du modèle est piloté par HF_HOME (bundlé dans l'image, offline).
        _model = CrossEncoder(_MODEL_NAME, device="cpu")
    return _model


def rerank(query: str, candidates: list[dict[str, Any]], top_k: int = 8
           ) -> list[dict[str, Any]]:
    """Ré-ordonne finement des candidats.

    `candidates` : liste de {id, payload{text,...}, ...}. Ajoute `rerank_score`
    à chaque candidat, trie par score décroissant et renvoie les `top_k` meilleurs.

    Note : pour bge-reranker-v2-m3 (1 sortie), CrossEncoder.predict applique déjà
    une sigmoïde — le score est donc directement une probabilité dans [0,1].
    """
    if not candidates:
        return []
    model = _get_model()
    pairs = [(query, c["payload"].get("text", "")) for c in candidates]
    scores = model.predict(pairs)  # déjà dans [0,1] (sigmoïde interne)

    scored = []
    for c, s in zip(candidates, scores):
        c = {**c, "rerank_score": max(0.0, min(1.0, float(s)))}
        scored.append(c)
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    return scored[:top_k]
