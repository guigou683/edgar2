"""Analyse de fichiers -> chunks (brique 4b).

Dispatch selon le type de fichier et la stratégie de la base :
  - documents (pdf, docx, html, pptx, xlsx, md, txt, code…) -> unstructured,
    stratégie `fast` (couche texte native) ou `ocr_only` (OCR Tesseract) ;
  - images (.png/.jpg/.tiff…) -> **OCR forcé** même en base `fast`.

Renvoie une liste de core.ingest.Chunk (texte + métadonnées fichier/page/section).
"""
from __future__ import annotations

from pathlib import Path

# Extensions image : OCR forcé quelle que soit la stratégie de la base.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"}


def parse_file(path: str, file_name: str, strategy: str = "fast") -> list:
    """Analyse un fichier en chunks. `strategy` ∈ {fast, ocr_only}."""
    ext = Path(file_name).suffix.lower()
    if ext in IMAGE_EXTS:
        from core.parsers import images
        return images.ocr_image(path, file_name)
    if ext == ".pdf":
        from core.parsers import pdf
        return pdf.parse_pdf(path, file_name, strategy)
    from core.parsers import document
    return document.parse_document(path, file_name, strategy)
