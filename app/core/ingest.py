"""Orchestration de l'indexation (vectorisation + tagging) — brique 4a.

Reçoit des chunks déjà découpés (texte + métadonnées fichier/page/section),
et pour chacun :
  1. extrait des mots-clés (YAKE) ;
  2. construit le texte enrichi (chunk + mots-clés) ;
  3. calcule l'embedding dense (bge-m3, par lots) ;
  4. calcule le vecteur creux BM25 (FastEmbed) ;
  5. upsert dans la collection Qdrant de la base (vecteurs dense + sparse).

Déduplication par empreinte de fichier : un document dont le hash est déjà
présent dans la collection n'est pas réindexé.

Le parsing fichier -> chunks (unstructured + OCR) est fourni par la brique 4b ;
ce module est agnostique de la source des chunks.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core import keywords as kw
from core import llm, sparse, vectorstore

# Espace de noms pour des identifiants de points déterministes (upsert idempotent).
_NS = uuid.UUID("e0c1a2b3-d4e5-46f7-8899-aabbccddeeff")


@dataclass
class Chunk:
    """Un fragment de document à indexer."""
    text: str
    file: str
    page: Optional[int] = None
    section: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


def file_hash(data: bytes) -> str:
    """Empreinte SHA-256 d'un fichier (déduplication)."""
    return hashlib.sha256(data).hexdigest()


def is_indexed(base_id: str, doc_hash: str) -> bool:
    """Vrai si un document de ce hash est déjà présent dans la collection."""
    from qdrant_client import models as qm
    client = vectorstore.get_client()
    name = vectorstore.collection_name(base_id)
    if not client.collection_exists(name):
        return False
    hits, _ = client.scroll(
        collection_name=name,
        scroll_filter=qm.Filter(must=[qm.FieldCondition(
            key="doc_hash", match=qm.MatchValue(value=doc_hash))]),
        limit=1,
    )
    return len(hits) > 0


def index_chunks(base_id: str, doc_hash: str, chunks: list[Chunk],
                 top_keywords: int = 8, skip_if_indexed: bool = True) -> dict[str, Any]:
    """Indexe une liste de chunks d'un même document. Renvoie un petit rapport."""
    if skip_if_indexed and is_indexed(base_id, doc_hash):
        return {"indexed": 0, "skipped": True, "reason": "document déjà indexé"}

    if not chunks:
        return {"indexed": 0, "skipped": False, "reason": "aucun chunk"}

    # 1-2. Mots-clés + texte enrichi (vectorisé ET indexé BM25).
    keyword_lists = [kw.extract_keywords(c.text, top_keywords) for c in chunks]
    enriched = [kw.augment_text(c.text, kws) for c, kws in zip(chunks, keyword_lists)]

    # 3. Embeddings denses (par lots) sur le texte enrichi.
    dense_vecs = llm.embed_texts(enriched)

    # 4. Vecteurs creux BM25 sur le même texte enrichi.
    sparse_vecs = sparse.encode_documents(enriched)

    # 5. Construction des points et upsert.
    points = []
    for i, (c, kws, dvec, (sidx, sval)) in enumerate(
            zip(chunks, keyword_lists, dense_vecs, sparse_vecs)):
        pid = str(uuid.uuid5(_NS, f"{doc_hash}:{i}"))
        points.append({
            "id": pid,
            "dense": dvec,
            "sparse_indices": sidx,
            "sparse_values": sval,
            "payload": {
                "text": c.text,            # texte original (citation)
                "text_indexed": enriched[i],  # texte enrichi (traçabilité)
                "keywords": kws,
                "file": c.file,
                "page": c.page,
                "section": c.section,
                "doc_hash": doc_hash,
                "base_id": base_id,
                "chunk_index": i,
                **c.extra,
            },
        })
    vectorstore.upsert_chunks(base_id, points)
    pages = len({c.page for c in chunks if c.page is not None})
    ocr_used = any(c.extra.get("ocr") for c in chunks)
    return {"indexed": len(points), "pages": pages, "skipped": False,
            "reason": "", "ocr": ocr_used}


def ingest_file(base_id: str, file_path: str, file_name: Optional[str] = None,
                strategy: str = "fast", top_keywords: int = 8,
                skip_if_indexed: bool = True) -> dict[str, Any]:
    """Indexe un fichier complet : lecture -> hash -> parsing -> chunks -> index.

    La déduplication s'appuie sur le hash du fichier (avant tout parsing coûteux).
    Les images forcent l'OCR quelle que soit la stratégie (cf. parsers.parse_file).
    """
    p = Path(file_path)
    file_name = file_name or p.name
    data = p.read_bytes()
    doc_hash = file_hash(data)

    if skip_if_indexed and is_indexed(base_id, doc_hash):
        return {"indexed": 0, "skipped": True, "reason": "document déjà indexé"}

    # Import paresseux pour éviter tout cycle (parsers importe ingest.Chunk).
    from core.parsers import parse_file
    chunks = parse_file(file_path, file_name, strategy)
    if not chunks:
        return {"indexed": 0, "pages": 0, "skipped": False,
                "reason": "aucun texte extrait (type non pris en charge, fichier vide "
                          "ou scan sans OCR : essayer la stratégie OCR)"}
    return index_chunks(base_id, doc_hash, chunks, top_keywords, skip_if_indexed=False)


def reindex_file(base_id: str, file_path: str, file_name: Optional[str] = None,
                 strategy: str = "fast", top_keywords: int = 8) -> dict[str, Any]:
    """Ré-analyse un document : purge ses points existants puis réindexe."""
    name = file_name or Path(file_path).name
    vectorstore.delete_by_file(base_id, name)
    return ingest_file(base_id, file_path, file_name=name, strategy=strategy,
                       top_keywords=top_keywords, skip_if_indexed=False)
