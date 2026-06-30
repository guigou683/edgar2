"""Accès à Qdrant : collections par base, vecteurs nommés dense + sparse.

Chaque base documentaire possède **sa** collection (cloisonnement total).
Chaque point porte :
  - un vecteur dense  "dense"  (bge-m3, 1024 dim, distance cosinus) ;
  - un vecteur creux  "sparse" (BM25, IDF calculé côté serveur Qdrant).

La recherche hybride utilise la **Query API native** de Qdrant
(`prefetch` dense + `prefetch` sparse + `FusionQuery(RRF)`), avec des leviers
pour isoler le dense seul ou le BM25 seul (panneau d'expérimentation).
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from core.config import settings

# Noms des vecteurs (référencés à l'indexation comme à la recherche).
DENSE = "dense"
SPARSE = "sparse"
DENSE_SIZE = 1024  # bge-m3, imposé

_client: Optional[QdrantClient] = None


def get_client() -> QdrantClient:
    """Client Qdrant partagé (connexion paresseuse)."""
    global _client
    if _client is None:
        _client = QdrantClient(url=settings.QDRANT_URL, timeout=60.0)
    return _client


def collection_name(base_id: str) -> str:
    """Nom de collection dérivé de l'identifiant de base (préfixe edgar2_)."""
    return f"edgar2_{base_id}"


# --------------------------------------------------------------------------
# Cycle de vie des collections
# --------------------------------------------------------------------------
def create_collection(base_id: str) -> None:
    """Crée la collection d'une base avec les deux vecteurs nommés + index payload."""
    client = get_client()
    name = collection_name(base_id)
    client.create_collection(
        collection_name=name,
        vectors_config={DENSE: qm.VectorParams(size=DENSE_SIZE, distance=qm.Distance.COSINE)},
        sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
    )
    # Index payload utiles au filtrage / à la déduplication / à l'affichage.
    for field in ("file", "doc_hash", "keywords"):
        client.create_payload_index(name, field, qm.PayloadSchemaType.KEYWORD)


def delete_collection(base_id: str) -> None:
    client = get_client()
    name = collection_name(base_id)
    if client.collection_exists(name):
        client.delete_collection(name)


def collection_exists(base_id: str) -> bool:
    return get_client().collection_exists(collection_name(base_id))


def count_points(base_id: str) -> int:
    name = collection_name(base_id)
    client = get_client()
    if not client.collection_exists(name):
        return 0
    return client.count(name, exact=True).count


# --------------------------------------------------------------------------
# Indexation
# --------------------------------------------------------------------------
def upsert_chunks(base_id: str, points: Sequence[dict[str, Any]]) -> None:
    """Insère/maj des points.

    Chaque élément de `points` :
        {
          "id": str|int,
          "dense": [float, ...],                  # 1024 dim
          "sparse_indices": [int, ...],
          "sparse_values": [float, ...],
          "payload": {...},
        }
    """
    client = get_client()
    structs = [
        qm.PointStruct(
            id=p["id"],
            vector={
                DENSE: p["dense"],
                SPARSE: qm.SparseVector(indices=p["sparse_indices"], values=p["sparse_values"]),
            },
            payload=p.get("payload", {}),
        )
        for p in points
    ]
    client.upsert(collection_name(base_id), points=structs)


# --------------------------------------------------------------------------
# Recherche
# --------------------------------------------------------------------------
def hybrid_search(
    base_id: str,
    dense_vec: Optional[list[float]] = None,
    sparse_indices: Optional[list[int]] = None,
    sparse_values: Optional[list[float]] = None,
    limit: int = 20,
    use_dense: bool = True,
    use_sparse: bool = True,
) -> list[dict[str, Any]]:
    """Recherche par requête, avec leviers dense / BM25.

    - use_dense & use_sparse  -> hybride (RRF natif Qdrant) ;
    - use_dense seul          -> vectoriel pur ;
    - use_sparse seul         -> BM25 pur.

    Renvoie une liste de {id, score, payload} ordonnée par pertinence.
    """
    client = get_client()
    name = collection_name(base_id)
    sparse_q = (
        qm.SparseVector(indices=sparse_indices or [], values=sparse_values or [])
        if sparse_indices else None
    )

    dense_ok = use_dense and dense_vec is not None
    sparse_ok = use_sparse and sparse_q is not None

    if dense_ok and sparse_ok:
        # Hybride : deux prefetch fusionnés par RRF.
        resp = client.query_points(
            collection_name=name,
            prefetch=[
                qm.Prefetch(query=dense_vec, using=DENSE, limit=limit),
                qm.Prefetch(query=sparse_q, using=SPARSE, limit=limit),
            ],
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
    elif dense_ok:
        resp = client.query_points(name, query=dense_vec, using=DENSE,
                                   limit=limit, with_payload=True)
    elif sparse_ok:
        resp = client.query_points(name, query=sparse_q, using=SPARSE,
                                   limit=limit, with_payload=True)
    else:
        raise ValueError("hybrid_search : aucune modalité de recherche active")

    return [{"id": p.id, "score": p.score, "payload": p.payload or {}} for p in resp.points]
