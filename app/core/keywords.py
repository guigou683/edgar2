"""Extraction de mots-clés des chunks (tagging — évolution v2).

Extraction légère **locale et déterministe** via YAKE (offline).

Les mots-clés sont stockés en métadonnées ET injectés dans le texte vectorisé
(dense) et indexé (BM25) pour densifier le sens et améliorer le rappel lexical.
"""
from __future__ import annotations

import yake

# Extracteur YAKE français (unigrammes + bigrammes). Réutilisé entre appels.
_extractor = yake.KeywordExtractor(lan="fr", n=2, dedupLim=0.9, top=20)


def extract_keywords(text: str, top_n: int = 8) -> list[str]:
    """Extrait jusqu'à top_n mots-clés d'un texte (YAKE, score croissant = pertinent)."""
    text = (text or "").strip()
    if len(text) < 20:
        return []
    pairs = _extractor.extract_keywords(text)  # [(mot, score), ...]
    pairs.sort(key=lambda kv: kv[1])           # plus petit score = plus pertinent
    seen, out = set(), []
    for kw, _score in pairs:
        k = kw.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(kw.strip())
        if len(out) >= top_n:
            break
    return out


def augment_text(text: str, keywords: list[str]) -> str:
    """Texte enrichi pour la vectorisation/indexation (chunk + mots-clés)."""
    if not keywords:
        return text
    return f"{text}\n\nMots-clés : {', '.join(keywords)}"
