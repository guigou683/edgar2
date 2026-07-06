"""Reranking par cross-encoder **ONNX** (FastEmbed), exécuté sur CPU.

Modèle : `jinaai/jina-reranker-v2-base-multilingual` — multilingue (français),
beaucoup plus rapide sur CPU que bge-reranker-v2-m3 (torch), pour une qualité
comparable. N'intervient qu'à la recherche : aucun impact sur l'indexation.

Le score brut du cross-encoder est ramené dans [0,1] par une sigmoïde, ce qui
rend le seuil de pertinence interprétable (refus d'inventer).
"""
from __future__ import annotations

import math
from typing import Any, Optional

from core.config import settings

_MODEL_NAME = "jinaai/jina-reranker-v2-base-multilingual"
_model: Optional[Any] = None


def _cache_dir() -> str:
    return str(settings.MODELS_DIR / "fastembed")


def _get_model():
    """Charge le cross-encoder ONNX (singleton paresseux), depuis le cache bundlé."""
    global _model
    if _model is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _model = TextCrossEncoder(model_name=_MODEL_NAME, cache_dir=_cache_dir())
    return _model


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def rerank(query: str, candidates: list[dict[str, Any]], top_k: int = 8
           ) -> list[dict[str, Any]]:
    """Ré-ordonne des candidats et renvoie les top_k, avec `rerank_score` ∈ [0,1]."""
    if not candidates:
        return []
    model = _get_model()
    docs = [c["payload"].get("text", "") for c in candidates]
    scores = list(model.rerank(query, docs))  # scores bruts (logits)

    scored = []
    for c, s in zip(candidates, scores):
        c = {**c, "rerank_score": _sigmoid(float(s))}
        scored.append(c)
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    return scored[:top_k]
