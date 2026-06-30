"""Pipeline d'interrogation — cœur de la v2 (brique 5).

À chaque question :
  1. Re-prompt multi-requêtes (LLM local) — optionnel.
  2. Recherche hybride par requête (dense + BM25 + RRF natif Qdrant) — leviers
     hybride / dense seul / BM25 seul.
  3. Fusion inter-requêtes (RRF sur les rangs, dédup par id de chunk).
  4. Reranking (cross-encoder bge-reranker-v2-m3, CPU) — optionnel.
  5. Seuil de pertinence : sous le seuil, le passage est écarté ; si plus rien ne
     passe → « information non trouvée » (refus d'inventer).

Tous les leviers sont réglables (panneau d'expérimentation). Un diagnostic
détaillé est renvoyé pour mesurer l'effet de chaque interrupteur.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from core import llm, rerank, sparse, vectorstore

# Modes de recherche (toggle « Recherche hybride » + sous-toggle « BM25 »).
MODE_HYBRID = "hybrid"   # dense + BM25 + RRF
MODE_DENSE = "dense"     # vectoriel seul
MODE_BM25 = "bm25"       # lexical seul


@dataclass
class SearchParams:
    """Leviers de recherche (par session/utilisateur ou défauts globaux)."""
    mode: str = MODE_HYBRID
    use_reprompt: bool = True
    n_reformulations: int = 3
    use_rerank: bool = True
    top_k: int = 8
    k_candidates: int = 20
    threshold: float = 0.3
    search_mode: bool = False           # extraits seuls (sans génération) — brique 6
    llm_model: str | None = None


def _rrf_fuse(result_lists: list[list[dict[str, Any]]], k: int = 60
              ) -> list[dict[str, Any]]:
    """Fusion RRF de plusieurs listes classées ; dédup par id, score RRF cumulé."""
    agg: dict[Any, dict[str, Any]] = {}
    for results in result_lists:
        for rank, r in enumerate(results):
            rid = r["id"]
            contrib = 1.0 / (k + rank + 1)
            if rid not in agg:
                agg[rid] = {**r, "rrf_score": 0.0}
            agg[rid]["rrf_score"] += contrib
    fused = list(agg.values())
    fused.sort(key=lambda r: r["rrf_score"], reverse=True)
    return fused


def retrieve(base_id: str, question: str, params: SearchParams | None = None
             ) -> dict[str, Any]:
    """Exécute le pipeline et renvoie {results, diagnostics}."""
    params = params or SearchParams()
    diag: dict[str, Any] = {"timings_ms": {}, "queries": [], "counts": {}}

    # 1. Reformulations (la question d'origine est toujours conservée).
    t0 = time.perf_counter()
    queries = [question]
    if params.use_reprompt:
        queries += llm.reformulate(question, n=params.n_reformulations,
                                   model=params.llm_model)
    diag["queries"] = queries
    diag["timings_ms"]["reprompt"] = round((time.perf_counter() - t0) * 1000)

    use_dense = params.mode in (MODE_HYBRID, MODE_DENSE)
    use_sparse = params.mode in (MODE_HYBRID, MODE_BM25)

    # 2. Recherche hybride par requête.
    t0 = time.perf_counter()
    per_query_results: list[list[dict[str, Any]]] = []
    for q in queries:
        dvec = llm.embed_query(q, model=None) if use_dense else None
        sidx = sval = None
        if use_sparse:
            sidx, sval = sparse.encode_query(q)
        res = vectorstore.hybrid_search(
            base_id, dense_vec=dvec, sparse_indices=sidx, sparse_values=sval,
            limit=params.k_candidates, use_dense=use_dense, use_sparse=use_sparse,
        )
        per_query_results.append(res)
    diag["timings_ms"]["search"] = round((time.perf_counter() - t0) * 1000)

    # 3. Fusion inter-requêtes (dédup par id).
    fused = _rrf_fuse(per_query_results)
    diag["counts"]["candidats_fusionnes"] = len(fused)

    if not fused:
        diag["reason"] = "aucun candidat"
        return {"results": [], "diagnostics": diag}

    # 4. Reranking (optionnel) puis 5. seuil de pertinence.
    if params.use_rerank:
        t0 = time.perf_counter()
        reranked = rerank.rerank(question, fused[:max(params.k_candidates, params.top_k * 3)],
                                 top_k=len(fused))
        diag["timings_ms"]["rerank"] = round((time.perf_counter() - t0) * 1000)
        diag["scores_avant_rerank"] = [round(r["rrf_score"], 4) for r in fused[:params.top_k]]
        diag["scores_apres_rerank"] = [round(r["rerank_score"], 4) for r in reranked[:params.top_k]]
        kept = [r for r in reranked if r["rerank_score"] >= params.threshold][:params.top_k]
        score_key = "rerank_score"
    else:
        # Sans reranker : seuil appliqué au score RRF normalisé (max -> 1).
        top = fused[0]["rrf_score"] or 1.0
        for r in fused:
            r["norm_score"] = r["rrf_score"] / top
        kept = [r for r in fused if r["norm_score"] >= params.threshold][:params.top_k]
        score_key = "norm_score"

    diag["counts"]["retenus"] = len(kept)
    if not kept:
        diag["reason"] = "sous le seuil de pertinence"

    results = [{
        "id": r["id"],
        "score": round(float(r.get(score_key, 0.0)), 4),
        "payload": r["payload"],
    } for r in kept]
    return {"results": results, "diagnostics": diag}
