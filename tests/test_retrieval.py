"""Test d'intégration de la brique 5 (retrieval), en conteneur.

Couvre : recherche hybride multi-leviers, reranking (correction de l'ordre),
seuil de pertinence (refus d'inventer hors corpus), re-prompt multi-requêtes,
et diagnostic. Prérequis : Ollama (bge-m3 + mistral:7b) + Qdrant ; reranker bundlé.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from core import bases, ingest, retrieval  # noqa: E402
from core.retrieval import SearchParams  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK  ' if cond else 'ECHEC'}] {label}")
    if not cond:
        _failures.append(label)


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
        b = bases.create_base("Test Retrieval", "5", parse_strategy="fast")
        bid = b["id"]
        chunks = [ingest.Chunk(text=t, file="corpus.txt", page=1, section=f"§{i+1}")
                  for i, t in enumerate(DOCS)]
        ingest.index_chunks(bid, ingest.file_hash("\n".join(DOCS).encode()), chunks)

        Q = "Comment détecter un sous-marin ?"

        # --- Reranking : l'ordre final met le chunk 'sonar' en tête ---
        p = SearchParams(use_reprompt=False, use_rerank=True, threshold=0.3, top_k=3)
        out = retrieval.retrieve(bid, Q, p)
        res = out["results"]
        check("rerank : au moins un résultat", len(res) > 0)
        if res:
            print("       top:", res[0]["payload"]["text"][:55], f"({res[0]['score']})")
            check("rerank : 'sonar' en tête (ordre corrigé)", "sonar" in res[0]["payload"]["text"].lower())
        check("diagnostic : scores avant/après rerank présents",
              "scores_avant_rerank" in out["diagnostics"] and "scores_apres_rerank" in out["diagnostics"])

        # --- Seuil de pertinence : question hors corpus -> refus d'inventer ---
        p_strict = SearchParams(use_reprompt=False, use_rerank=True, threshold=0.5, top_k=3)
        hors = retrieval.retrieve(bid, "Quelle est la recette de la tarte aux pommes ?", p_strict)
        check("hors corpus : aucun passage retenu (information non trouvée)",
              len(hors["results"]) == 0)
        print("       raison:", hors["diagnostics"].get("reason"))

        # --- Leviers : dense seul / BM25 seul / hybride renvoient des résultats ---
        for mode in (retrieval.MODE_DENSE, retrieval.MODE_BM25, retrieval.MODE_HYBRID):
            o = retrieval.retrieve(bid, Q, SearchParams(mode=mode, use_reprompt=False,
                                                        use_rerank=False, threshold=0.0, top_k=3))
            check(f"levier mode={mode} : résultats", len(o["results"]) > 0)

        # --- Re-prompt multi-requêtes (LLM mistral) ---
        pr = SearchParams(use_reprompt=True, n_reformulations=2, use_rerank=True, threshold=0.3, top_k=3)
        ro = retrieval.retrieve(bid, Q, pr)
        variants = ro["diagnostics"]["queries"]
        print("       variantes:", variants)
        check("re-prompt : variantes générées (> question seule)", len(variants) >= 2)
        check("re-prompt : résultats pertinents", len(ro["results"]) > 0)
        check("diagnostic : timings présents", "timings_ms" in ro["diagnostics"])

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
