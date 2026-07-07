"""Orchestration RAG de bout en bout (brique 6).

Relie le pipeline d'interrogation (retrieval) à la génération :
  - assemble les extraits retenus en **contexte numéroté** ;
  - impose un **prompt système** : réponse en français, **uniquement** d'après le
    contexte, **citer les sources [n]**, signaler toute information absente ;
  - **refus d'inventer** : si aucun passage ne passe le seuil, renvoie « information
    non trouvée » sans appeler le LLM.

Les sources renvoyées à l'UI sont reconstruites depuis les **métadonnées** des
passages (fichier, page, section), jamais depuis le texte généré.
"""
from __future__ import annotations

from typing import Any, Iterator

from core import llm
from core.retrieval import SearchParams, retrieve

SYSTEM_PROMPT = (
    "Tu es EDGAR, un assistant documentaire de la Marine nationale. "
    "Réponds en français, dans un registre clair et technique. "
    "Tu réponds UNIQUEMENT à partir des extraits numérotés fournis dans le contexte. "
    "Cite systématiquement tes sources avec leur numéro entre crochets, par exemple [1] ou [2]. "
    "Si l'information demandée n'est pas présente dans le contexte, indique-le explicitement "
    "et n'invente jamais de réponse."
)

NOT_FOUND_MESSAGE = (
    "Information non trouvée dans le corpus documentaire. "
    "Aucun extrait suffisamment pertinent n'a été identifié pour répondre à cette question."
)


def _assemble_context(results: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Construit le contexte numéroté et la liste des sources (depuis les métadonnées)."""
    blocks, sources = [], []
    for i, r in enumerate(results, 1):
        p = r.get("payload", {})
        text = p.get("text", "")
        blocks.append(f"[{i}] {text}")
        sources.append({
            "n": i,
            "file": p.get("file"),
            "page": p.get("page"),
            "section": p.get("section"),
            "score": r.get("score"),
            "keywords": p.get("keywords", []),
            "text": text,
        })
    return "\n\n".join(blocks), sources


def _build_prompt(question: str, context: str) -> str:
    return (f"Contexte (extraits numérotés) :\n{context}\n\n"
            f"Question : {question}\n\n"
            f"Réponds en français en t'appuyant uniquement sur ces extraits et en citant "
            f"les sources entre crochets [n].")


def prepare(base_id: str, question: str, params: SearchParams | None = None) -> dict[str, Any]:
    """Exécute le retrieval et prépare la réponse.

    Renvoie un dict :
      - found        : au moins un extrait pertinent ?
      - sources      : liste des sources (métadonnées + extrait) ;
      - diagnostics  : diagnostic du pipeline ;
      - search_mode  : si vrai, pas de génération (extraits seuls) ;
      - prompt       : prompt prêt pour le LLM (si génération) ou None.
    """
    params = params or SearchParams()
    return assemble(question, retrieve(base_id, question, params), params)


def assemble(question: str, out: dict[str, Any], params: SearchParams) -> dict[str, Any]:
    """Transforme le résultat du retrieval en {found, sources, prompt, ...}."""
    results = out.get("results", [])
    diagnostics = out.get("diagnostics", {})
    if not results:
        return {"found": False, "sources": [], "diagnostics": diagnostics,
                "search_mode": params.search_mode, "prompt": None}
    context, sources = _assemble_context(results)
    prompt = None if params.search_mode else _build_prompt(question, context)
    return {"found": True, "sources": sources, "diagnostics": diagnostics,
            "search_mode": params.search_mode, "prompt": prompt}


def generate_answer(prompt: str, model: str | None = None) -> Iterator[str]:
    """Génère la réponse en streaming (jetons) à partir du prompt assemblé."""
    yield from llm.generate_stream(prompt, system=SYSTEM_PROMPT, model=model)
