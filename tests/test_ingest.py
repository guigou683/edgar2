"""Test d'intégration de la brique 4a (vectorisation + tagging), Ollama+Qdrant réels.

Prérequis : Ollama (modèle bge-m3 présent) et Qdrant accessibles.
Depuis l'hôte :

    OLLAMA_URL=http://localhost:11435 QDRANT_URL=http://localhost:7333 \
    EDGAR_DATA_DIR=<tmp> EDGAR_DOCS_DIR=<tmp/docs> python tests/test_ingest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from core import bases, ingest, keywords, llm, sparse, vectorstore  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK  ' if cond else 'ECHEC'}] {label}")
    if not cond:
        _failures.append(label)


# Mini-corpus de démonstration (français, thématique Marine).
DOCS = [
    "La frégate multi-missions FREMM est un navire de combat de la Marine nationale, "
    "conçu pour la lutte anti-sous-marine et la défense aérienne.",
    "Le sonar remorqué permet la détection des sous-marins à grande distance grâce à "
    "l'analyse des signaux acoustiques sous-marins.",
    "Le porte-avions Charles de Gaulle assure la projection de puissance aérienne ; "
    "son groupe aérien embarqué comprend des avions de chasse Rafale Marine.",
]


def main() -> int:
    bid = None
    try:
        b = bases.create_base("Test Ingestion", "mini-corpus", parse_strategy="fast")
        bid = b["id"]

        # --- YAKE : extraction de mots-clés français ---
        kws = keywords.extract_keywords(DOCS[0], top_n=6)
        check("YAKE : mots-clés extraits", len(kws) > 0)
        check("YAKE : terme pertinent présent",
              any("fr" in k.lower() or "navire" in k.lower() or "marine" in k.lower() for k in kws))
        print("       mots-clés:", kws)

        # --- Embeddings bge-m3 : dimension 1024 ---
        vec = llm.embed_query("frégate de combat")
        check("bge-m3 : dimension 1024", len(vec) == 1024)

        # --- Sparse BM25 : vecteur non vide ---
        sidx, sval = sparse.encode_query("sonar sous-marin")
        check("BM25 : indices non vides", len(sidx) > 0 and len(sidx) == len(sval))

        # --- Indexation du mini-corpus ---
        chunks = [ingest.Chunk(text=t, file="corpus.txt", page=1, section=f"§{i+1}")
                  for i, t in enumerate(DOCS)]
        doc_hash = ingest.file_hash("\n".join(DOCS).encode("utf-8"))
        rep = ingest.index_chunks(bid, doc_hash, chunks)
        check("indexation : 3 chunks", rep["indexed"] == 3)
        check("comptage Qdrant = 3", vectorstore.count_points(bid) == 3)

        # --- Déduplication : ré-indexation ignorée ---
        rep2 = ingest.index_chunks(bid, doc_hash, chunks)
        check("déduplication : ré-indexation ignorée", rep2["skipped"] is True)
        check("comptage inchangé après dédup", vectorstore.count_points(bid) == 3)

        # --- Recherche hybride sémantique réelle ---
        q = "Comment détecter un sous-marin ?"
        qvec = llm.embed_query(q)
        qidx, qval = sparse.encode_query(q)
        res = vectorstore.hybrid_search(bid, dense_vec=qvec, sparse_indices=qidx,
                                        sparse_values=qval, limit=3)
        for rang, r in enumerate(res, 1):
            print(f"       #{rang} ({r['score']:.4f})", r["payload"]["text"][:55], "...")
        # Au stade retrieval, on valide le RAPPEL (le bon chunk est récupéré) ;
        # l'ordre fin est affiné par le reranker (brique 5).
        textes = " ".join(r["payload"]["text"].lower() for r in res)
        check("recherche hybride : chunk 'sonar' rappelé", "sonar" in textes)
        check("payload : mots-clés stockés", bool(res and res[0]["payload"].get("keywords")))
        check("payload : métadonnées source", bool(res and res[0]["payload"].get("section")))

        # --- Comparaison dense seul vs BM25 seul (leviers du panneau) ---
        dense_res = vectorstore.hybrid_search(bid, dense_vec=qvec, use_sparse=False, limit=3)
        bm25_res = vectorstore.hybrid_search(bid, sparse_indices=qidx, sparse_values=qval,
                                             use_dense=False, limit=3)
        check("dense seul renvoie des résultats", len(dense_res) > 0)
        check("BM25 seul renvoie des résultats", len(bm25_res) > 0)

    finally:
        if bid:
            try:
                bases.delete_base(bid, remove_documents=True)
            except Exception:
                pass

    print()
    if _failures:
        print(f"RESULTAT : {len(_failures)} echec(s).")
        return 1
    print("RESULTAT : tous les tests passent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
