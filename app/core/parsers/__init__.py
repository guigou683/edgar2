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

# Formats documentaires pris en charge (via unstructured / pdf).
DOC_EXTS = {
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".text", ".md", ".markdown",
    ".rst", ".html", ".htm", ".xml", ".epub", ".pptx", ".ppt", ".xlsx", ".xls",
    ".csv", ".tsv", ".eml", ".msg", ".json",
}

# Liste blanche complète : tout le reste (mp4, zip, exe…) est **ignoré** proprement,
# sans tentative d'indexation ni statut « échec ».
SUPPORTED_EXTS = IMAGE_EXTS | DOC_EXTS


def is_supported(file_name: str) -> bool:
    """Vrai si l'extension du fichier fait partie des formats pris en charge."""
    return Path(file_name).suffix.lower() in SUPPORTED_EXTS


def parse_file(path: str, file_name: str, strategy: str = "fast",
               ocr_workers: int = 1, progress=None) -> list:
    """Analyse un fichier en chunks. `strategy` ∈ {fast, ocr_only}.
    `ocr_workers` parallélise l'OCR des pages PDF sur plusieurs cœurs.
    `progress(stage, done, total)` remonte l'avancement OCR (PDF)."""
    ext = Path(file_name).suffix.lower()
    if ext in IMAGE_EXTS:
        from core.parsers import images
        return images.ocr_image(path, file_name)
    if ext == ".pdf":
        from core.parsers import pdf
        return pdf.parse_pdf(path, file_name, strategy, ocr_workers=ocr_workers, progress=progress)
    from core.parsers import document
    return document.parse_document(path, file_name, strategy)
