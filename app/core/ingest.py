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


def _keyword_lists(chunks: list[Chunk], top_keywords: int, yake_pool=None,
                   progress=None) -> list[list[str]]:
    """Mots-clés YAKE de chaque chunk. `yake_pool` (ProcessPool) parallélise sur
    plusieurs cœurs si fourni ; repli séquentiel en cas d'échec du pool.
    `progress(done, total)` est appelé au fil des chunks (suivi intra-fichier)."""
    texts = [c.text for c in chunks]
    n = len(texts)
    if yake_pool is not None and n > 1:
        try:
            out: list[list[str]] = []
            for i, kws in enumerate(
                    yake_pool.map(kw.extract_keywords, texts, [top_keywords] * n), 1):
                out.append(kws)
                if progress:
                    progress(i, n)
            return out
        except Exception:
            pass  # pool indisponible -> repli séquentiel
    out = []
    for i, t in enumerate(texts, 1):
        out.append(kw.extract_keywords(t, top_keywords))
        if progress:
            progress(i, n)
    return out


def prepare_document(base_id: str, doc_hash: str, chunks: list[Chunk],
                     top_keywords: int = 8, yake_pool=None, progress=None) -> dict[str, Any]:
    """Phase CPU (sans GPU) : mots-clés + texte enrichi + vecteurs creux BM25."""
    kw_progress = (lambda d, t: progress("keywords", d, t)) if progress else None
    keyword_lists = _keyword_lists(chunks, top_keywords, yake_pool, kw_progress)
    enriched = [kw.augment_text(c.text, kws) for c, kws in zip(chunks, keyword_lists)]
    sparse_vecs = sparse.encode_documents(enriched)
    return {"keyword_lists": keyword_lists, "enriched": enriched, "sparse_vecs": sparse_vecs}


def embed_and_upsert(base_id: str, doc_hash: str, chunks: list[Chunk],
                     prep: dict[str, Any], delete_file: Optional[str] = None,
                     progress=None) -> dict[str, Any]:
    """Phase GPU : embeddings denses (Ollama) + construction des points + upsert Qdrant.

    `delete_file` (ré-analyse) purge les anciens points juste avant l'upsert."""
    keyword_lists, enriched, sparse_vecs = (
        prep["keyword_lists"], prep["enriched"], prep["sparse_vecs"])
    emb_progress = (lambda d, t: progress("index", d, t)) if progress else None
    dense_vecs = llm.embed_texts(enriched, progress=emb_progress)  # seul appel GPU
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
    if delete_file:
        vectorstore.delete_by_file(base_id, delete_file)
    vectorstore.upsert_chunks(base_id, points)
    pages = len({c.page for c in chunks if c.page is not None})
    ocr_used = any(c.extra.get("ocr") for c in chunks)
    return {"indexed": len(points), "pages": pages, "skipped": False,
            "reason": "", "ocr": ocr_used}


def index_chunks(base_id: str, doc_hash: str, chunks: list[Chunk],
                 top_keywords: int = 8, skip_if_indexed: bool = True) -> dict[str, Any]:
    """Indexe une liste de chunks d'un même document (prepare CPU puis embed GPU)."""
    if skip_if_indexed and is_indexed(base_id, doc_hash):
        return {"indexed": 0, "skipped": True, "reason": "document déjà indexé"}
    if not chunks:
        return {"indexed": 0, "skipped": False, "reason": "aucun chunk"}
    prep = prepare_document(base_id, doc_hash, chunks, top_keywords)
    return embed_and_upsert(base_id, doc_hash, chunks, prep)


def prepare_file(base_id: str, file_path: str, file_name: Optional[str] = None,
                 strategy: str = "fast", top_keywords: int = 8, skip_if_indexed: bool = True,
                 yake_pool=None, ocr_workers: int = 1, progress=None) -> dict[str, Any]:
    """Phase CPU complète d'un fichier : lecture -> hash -> dedup -> parsing (OCR
    parallèle) -> mots-clés -> BM25. Renvoie un dict `status` (ok/skipped/empty)
    prêt pour la phase GPU (embed_and_upsert). `progress(stage, done, total)`
    remonte l'avancement intra-fichier (analyse / ocr / keywords)."""
    p = Path(file_path)
    file_name = file_name or p.name
    doc_hash = file_hash(p.read_bytes())
    if skip_if_indexed and is_indexed(base_id, doc_hash):
        return {"status": "skipped", "reason": "document déjà indexé", "doc_hash": doc_hash}
    if progress:
        progress("analyse", 0, 0)
    from core.parsers import parse_file
    chunks = parse_file(file_path, file_name, strategy, ocr_workers=ocr_workers, progress=progress)
    if not chunks:
        return {"status": "empty", "doc_hash": doc_hash,
                "reason": "aucun texte extrait (type non pris en charge, fichier vide "
                          "ou scan sans OCR : essayer la stratégie OCR)"}
    prep = prepare_document(base_id, doc_hash, chunks, top_keywords, yake_pool, progress)
    return {"status": "ok", "doc_hash": doc_hash, "chunks": chunks, "prep": prep}


def ingest_file(base_id: str, file_path: str, file_name: Optional[str] = None,
                strategy: str = "fast", top_keywords: int = 8,
                skip_if_indexed: bool = True) -> dict[str, Any]:
    """Indexe un fichier complet : lecture -> hash -> parsing -> chunks -> index.

    La déduplication s'appuie sur le hash du fichier (avant tout parsing coûteux).
    Les images forcent l'OCR quelle que soit la stratégie (cf. parsers.parse_file).
    """
    pf = prepare_file(base_id, file_path, file_name, strategy, top_keywords, skip_if_indexed)
    if pf["status"] == "skipped":
        return {"indexed": 0, "skipped": True, "reason": pf["reason"]}
    if pf["status"] == "empty":
        return {"indexed": 0, "pages": 0, "skipped": False, "reason": pf["reason"]}
    return embed_and_upsert(base_id, pf["doc_hash"], pf["chunks"], pf["prep"])


def reindex_file(base_id: str, file_path: str, file_name: Optional[str] = None,
                 strategy: str = "fast", top_keywords: int = 8) -> dict[str, Any]:
    """Ré-analyse un document : purge ses points existants puis réindexe."""
    name = file_name or Path(file_path).name
    pf = prepare_file(base_id, file_path, name, strategy, top_keywords, skip_if_indexed=False)
    if pf["status"] == "empty":
        vectorstore.delete_by_file(base_id, name)  # purge même si plus rien d'exploitable
        return {"indexed": 0, "pages": 0, "skipped": False, "reason": pf["reason"]}
    return embed_and_upsert(base_id, pf["doc_hash"], pf["chunks"], pf["prep"], delete_file=name)
