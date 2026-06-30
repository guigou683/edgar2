"""Encodeur BM25 (vecteurs creux) via FastEmbed, 100 % offline.

Produit des vecteurs creux indexés dans Qdrant sous le nom "sparse" (l'IDF est
calculé côté serveur Qdrant grâce au Modifier.IDF de la collection).

- Documents : term-frequencies (méthode embed).
- Requête   : présence des termes (méthode query_embed).

Le modèle "Qdrant/bm25" est purement lexical (tokeniseur + stop-words + stemmer),
sans réseau de neurones ; ses ressources sont bundlées dans l'image pour l'offline.
"""
from __future__ import annotations

from typing import Optional

from fastembed import SparseTextEmbedding

from core.config import settings

_MODEL_NAME = "Qdrant/bm25"
_model: Optional[SparseTextEmbedding] = None


def _cache_dir() -> str:
    """Dossier de cache du modèle (bundlé dans l'image pour l'offline)."""
    return str(settings.MODELS_DIR / "fastembed")


def _get_model() -> SparseTextEmbedding:
    """Charge le modèle BM25 (singleton paresseux), depuis le cache bundlé."""
    global _model
    if _model is None:
        _model = SparseTextEmbedding(model_name=_MODEL_NAME, cache_dir=_cache_dir())
    return _model


def encode_documents(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """Encode des documents en vecteurs creux. Renvoie [(indices, valeurs), ...]."""
    model = _get_model()
    result = []
    for emb in model.embed(texts):
        result.append((emb.indices.tolist(), emb.values.tolist()))
    return result


def encode_query(text: str) -> tuple[list[int], list[float]]:
    """Encode une requête en vecteur creux (présence des termes)."""
    model = _get_model()
    emb = next(iter(model.query_embed([text])))
    return emb.indices.tolist(), emb.values.tolist()
