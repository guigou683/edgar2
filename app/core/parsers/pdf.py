"""Analyse des PDF (chemin dédié, léger).

unstructured.partition.pdf impose la pile lourde `unstructured_inference`
(torch/detectron) même en stratégie `fast`. Pour rester léger et 100 % offline,
les PDF sont traités directement :
  - `fast`     : extraction de la couche texte native via pypdf, page par page ;
  - `ocr_only` : rendu des pages en images (pdf2image) puis OCR Tesseract fra+eng.

Le numéro de page est conservé pour la traçabilité des sources (aperçu PDF).
"""
from __future__ import annotations

from core.ingest import Chunk

MAX_CHARS = 1000
OVERLAP = 150


def _chunk_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[str]:
    """Découpe un texte en blocs ~max_chars, sur les frontières de paragraphes,
    avec un léger chevauchement."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > max_chars:
            chunks.append(buf)
            buf = (buf[-overlap:] + "\n\n" + p) if overlap else p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def _fast(path: str) -> list[tuple[int, str]]:
    """(page, texte) via la couche texte native (pypdf)."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    out = []
    for i, page in enumerate(reader.pages, 1):
        out.append((i, page.extract_text() or ""))
    return out


def _ocr(path: str) -> list[tuple[int, str]]:
    """(page, texte) via rendu image + OCR Tesseract (fra+eng)."""
    import pytesseract
    from pdf2image import convert_from_path
    out = []
    for i, img in enumerate(convert_from_path(path, dpi=200), 1):
        out.append((i, pytesseract.image_to_string(img, lang="fra+eng")))
    return out


def parse_pdf(path: str, file_name: str, strategy: str = "fast") -> list[Chunk]:
    """Analyse un PDF en chunks avec numéro de page."""
    pages = _ocr(path) if strategy == "ocr_only" else _fast(path)
    chunks: list[Chunk] = []
    for page_no, text in pages:
        for block in _chunk_text(text):
            chunks.append(Chunk(text=block, file=file_name, page=page_no, section=None))
    return chunks
