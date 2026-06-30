"""OCR d'images via Tesseract (langues fra+eng).

Les fichiers image forcent l'OCR (cf. parsers.parse_file), même dans une base
configurée en stratégie `fast`.
"""
from __future__ import annotations

from core.ingest import Chunk


def ocr_image(path: str, file_name: str) -> list[Chunk]:
    """Extrait le texte d'une image par OCR. Renvoie 0 ou 1 chunk."""
    import pytesseract
    from PIL import Image

    with Image.open(path) as img:
        text = pytesseract.image_to_string(img, lang="fra+eng")

    text = (text or "").strip()
    if not text:
        return []
    return [Chunk(text=text, file=file_name, page=1, section=None)]
