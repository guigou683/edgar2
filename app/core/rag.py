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
    "et n'invente jamais de réponse. "
    "Mets en forme ta réponse en Markdown. Pour toute énumération ou suite d'étapes, utilise "
    "une vraie liste Markdown : un élément par ligne, chaque ligne commençant par « - » "
    "(liste à puces) ou « 1. », « 2. », « 3. »… (liste numérotée), avec un retour à la ligne "
    "entre chaque élément. N'écris jamais plusieurs étapes à la suite sur une même ligne."
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


# --------------------------------------------------------------------------
# Résumé d'un document (tous ses extraits indexés)
# --------------------------------------------------------------------------
SUMMARY_SYSTEM = (
    "Tu es EDGAR, un assistant documentaire de la Marine nationale. "
    "Tu produis des synthèses fidèles, en français, uniquement à partir des extraits fournis. "
    "N'invente rien et n'ajoute aucune information absente des extraits."
)

# Budget de caractères transmis au LLM (garde-fou contexte pour un modèle 7B).
SUMMARY_MAX_CHARS = 12000
# Résumé « long » (map-reduce) : taille d'un segment et plafond du nombre de segments.
SUMMARY_LONG_SEGMENT = 8000
SUMMARY_LONG_MAX_SEGMENTS = 15


def build_summary_prompt(filename: str, payloads: list[dict[str, Any]]) -> tuple[str, bool]:
    """Assemble les extraits d'un document en un prompt de synthèse.

    Renvoie (prompt, tronqué) — `tronqué` indique que le document dépassait le budget."""
    parts, total, truncated = [], 0, False
    for p in payloads:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        page = p.get("page")
        block = (f"[p.{page}] " if page else "") + t
        if total + len(block) > SUMMARY_MAX_CHARS:
            truncated = True
            break
        parts.append(block)
        total += len(block)
    context = "\n\n".join(parts)
    prompt = (
        f"Document : « {filename} ».\n\n"
        f"Extraits indexés du document :\n{context}\n\n"
        "Rédige une synthèse structurée et fidèle de ce document en français : "
        "objet du document, points clés, et éléments notables. "
        "Appuie-toi uniquement sur les extraits ci-dessus."
    )
    return prompt, truncated


def summarize_stream(filename: str, payloads: list[dict[str, Any]],
                     model: str | None = None) -> Iterator[str]:
    """Génère en streaming la synthèse d'un document à partir de ses extraits."""
    prompt, _ = build_summary_prompt(filename, payloads)
    yield from llm.generate_stream(prompt, system=SUMMARY_SYSTEM, model=model)


def _summary_segments(payloads: list[dict[str, Any]]) -> list[str]:
    """Découpe les extraits (dans l'ordre) en segments bornés pour le map-reduce."""
    segs: list[str] = []
    cur: list[str] = []
    curlen = 0
    for p in payloads:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        page = p.get("page")
        block = (f"[p.{page}] " if page else "") + t
        if curlen + len(block) > SUMMARY_LONG_SEGMENT and cur:
            segs.append("\n\n".join(cur))
            cur, curlen = [], 0
            if len(segs) >= SUMMARY_LONG_MAX_SEGMENTS:
                break
        cur.append(block)
        curlen += len(block)
    if cur and len(segs) < SUMMARY_LONG_MAX_SEGMENTS:
        segs.append("\n\n".join(cur))
    return segs


def summarize_long_stream(filename: str, payloads: list[dict[str, Any]],
                          model: str | None = None) -> Iterator[str]:
    """Résumé « long » couvrant tout le document (map-reduce) : on résume chaque
    segment (map), puis on fusionne les résumés partiels en une synthèse finale
    streamée (reduce). Sur un document tenant en un seul segment, équivaut au résumé simple."""
    segs = _summary_segments(payloads)
    if len(segs) <= 1:
        yield from summarize_stream(filename, payloads, model=model)
        return
    partials: list[str] = []
    for i, seg in enumerate(segs, 1):
        prompt = (f"Document « {filename} » — partie {i}/{len(segs)}.\n\n"
                  f"Extraits :\n{seg}\n\n"
                  "Résume fidèlement cette partie en français (points clés), "
                  "uniquement d'après ces extraits.")
        partials.append(llm.generate(prompt, system=SUMMARY_SYSTEM, model=model))
    combined = "\n\n".join(f"[Partie {i}] {p.strip()}" for i, p in enumerate(partials, 1))
    final_prompt = (
        f"Document « {filename} ».\n\n"
        f"Résumés partiels des différentes parties du document :\n{combined}\n\n"
        "À partir de ces résumés partiels, rédige UNE synthèse structurée et fidèle de "
        "l'ensemble du document en français (objet, points clés, éléments notables). "
        "Fais une synthèse cohérente, sans répéter les parties une à une.")
    yield from llm.generate_stream(final_prompt, system=SUMMARY_SYSTEM, model=model)
