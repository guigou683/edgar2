"""Analyse de documents via `unstructured`, avec découpage structuré.

Deux stratégies (réglables par base) :
  - `fast`     : extraction de la couche texte native (pdfminer pour les PDF) ;
  - `ocr_only` : OCR Tesseract (langues fra+eng) pour scans/PDF image.

Le découpage respecte la structure (titres/sections) via `chunk_by_title`, avec
un chevauchement raisonnable, et conserve les métadonnées (page, section).
"""
from __future__ import annotations

from typing import Any

from core.ingest import Chunk

# Paramètres de découpage (chevauchement raisonnable).
MAX_CHARS = 1000
NEW_AFTER_N_CHARS = 800
OVERLAP = 150


def _meta(element: Any) -> dict:
    try:
        return element.metadata.to_dict()
    except Exception:
        return {}


def parse_document(path: str, file_name: str, strategy: str = "fast") -> list[Chunk]:
    """Analyse un document en chunks structurés avec métadonnées."""
    from unstructured.chunking.title import chunk_by_title
    from unstructured.partition.auto import partition

    # `languages` ne sert qu'aux chemins OCR ; sans effet en extraction native.
    elements = partition(filename=path, strategy=strategy, languages=["fra", "eng"])

    # Suit le dernier titre rencontré pour renseigner la section de chaque chunk.
    composites = chunk_by_title(
        elements,
        max_characters=MAX_CHARS,
        new_after_n_chars=NEW_AFTER_N_CHARS,
        overlap=OVERLAP,
    )

    chunks: list[Chunk] = []
    for comp in composites:
        text = (comp.text or "").strip()
        if not text:
            continue
        md = _meta(comp)
        # Section : titre de rattachement si disponible dans les éléments d'origine.
        section = None
        for orig in (md.get("orig_elements") or []):
            try:
                if type(orig).__name__ == "Title":
                    section = (orig.text or "").strip() or None
                    break
            except Exception:
                pass
        chunks.append(Chunk(
            text=text,
            file=file_name,
            page=md.get("page_number"),
            section=section,
        ))
    return chunks
