"""Analyse des PDF (chemin dédié, léger).

unstructured.partition.pdf impose la pile lourde `unstructured_inference`
(torch/detectron) même en stratégie `fast`. Pour rester léger et 100 % offline,
les PDF sont traités directement :
  - `fast`     : extraction de la couche texte native via pypdf, page par page.
                 Toute page sans couche texte exploitable (PDF scanné/image)
                 bascule automatiquement en OCR Tesseract fra+eng ;
  - `ocr_only` : rendu de toutes les pages en images (pdf2image) puis OCR.

Le numéro de page est conservé pour la traçabilité des sources (aperçu PDF).
"""
from __future__ import annotations

from core.ingest import Chunk

MAX_CHARS = 1000
OVERLAP = 150

# En dessous de ce nombre de caractères, une page est considérée « sans texte
# natif exploitable » (typiquement un scan/image) et bascule sur l'OCR.
OCR_MIN_CHARS = 10
OCR_DPI = 150  # 150 dpi : bon compromis vitesse/précision (vs 200, ~1,5× plus rapide)


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
    """(page, texte) via la couche texte native (pypdf).

    Chaque page est isolée : si pypdf échoue sur une page au flux malformé
    (ex. IndexError), on renvoie une page vide → elle basculera sur l'OCR au lieu
    de faire échouer tout le document."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    out = []
    for i, page in enumerate(reader.pages, 1):
        try:
            out.append((i, page.extract_text() or ""))
        except Exception:
            out.append((i, ""))  # page illisible -> vide -> OCR de secours
    return out


def _ocr_image(img) -> str:
    import pytesseract
    return pytesseract.image_to_string(img, lang="fra+eng")


def _ocr(path: str, workers: int = 1, progress=None) -> list[tuple[int, str]]:
    """(page, texte) via rendu image + OCR Tesseract (fra+eng), tout le document.
    `workers` > 1 : OCR des pages en parallèle (Tesseract libère le GIL).
    `progress(stage, done, total)` remonte l'avancement page par page."""
    from pdf2image import convert_from_path
    imgs = convert_from_path(path, dpi=OCR_DPI, thread_count=max(1, workers))
    total = len(imgs)
    if workers > 1 and total > 1:
        from concurrent.futures import ThreadPoolExecutor
        texts: list[str] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for done, t in enumerate(ex.map(_ocr_image, imgs), 1):
                texts.append(t)
                if progress:
                    progress("ocr", done, total)
        return [(i + 1, t) for i, t in enumerate(texts)]
    out = []
    for i, img in enumerate(imgs, 1):
        out.append((i, _ocr_image(img)))
        if progress:
            progress("ocr", i, total)
    return out


def _ocr_page(path: str, page_no: int) -> str:
    """OCR d'une seule page (1-based) : rend uniquement cette page puis Tesseract."""
    from pdf2image import convert_from_path
    imgs = convert_from_path(path, dpi=OCR_DPI, first_page=page_no, last_page=page_no)
    return _ocr_image(imgs[0]) if imgs else ""


def _fast_with_ocr_fallback(path: str, workers: int = 1,
                            progress=None) -> tuple[list[tuple[int, str]], set[int]]:
    """Couche texte native ; les pages sans texte exploitable passent à l'OCR.

    Couvre le PDF entièrement scanné (toutes les pages via OCR) comme le PDF
    mixte (OCR ciblé des seules pages images). Si l'OCR est indisponible, on
    conserve le résultat natif au lieu de faire échouer l'ingestion.

    Renvoie (pages, numéros des pages effectivement passées par l'OCR).
    """
    pages = _fast(path)
    empty = [idx for idx, (_, txt) in enumerate(pages)
             if len((txt or "").strip()) < OCR_MIN_CHARS]
    if not empty:
        return pages, set()
    try:
        if len(empty) == len(pages):
            ocr_pages = _ocr(path, workers, progress)  # aucune couche texte : rendu global
            return ocr_pages, {pn for pn, _ in ocr_pages}
        page_nos = [pages[idx][0] for idx in empty]   # PDF mixte : OCR ciblé, parallèle
        total = len(page_nos)
        texts: list[str] = []
        if workers > 1 and total > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for done, t in enumerate(ex.map(lambda pn: _ocr_page(path, pn), page_nos), 1):
                    texts.append(t)
                    if progress:
                        progress("ocr", done, total)
        else:
            for done, pn in enumerate(page_nos, 1):
                texts.append(_ocr_page(path, pn))
                if progress:
                    progress("ocr", done, total)
        for idx, txt in zip(empty, texts):
            pages[idx] = (pages[idx][0], txt)
        return pages, set(page_nos)
    except Exception:
        return _fast(path), set()   # OCR indisponible : on ne régresse pas le mode fast


def parse_pdf(path: str, file_name: str, strategy: str = "fast",
              ocr_workers: int = 1, progress=None) -> list[Chunk]:
    """Analyse un PDF en chunks avec numéro de page.

    Les chunks issus d'une page passée par l'OCR portent extra={"ocr": True},
    ce qui permet au registre d'afficher « fast (OCR auto) » le cas échéant.
    `ocr_workers` parallélise l'OCR des pages sur plusieurs cœurs.
    """
    if strategy == "ocr_only":
        pages, ocr_pages = _ocr(path, ocr_workers, progress), set()
    else:
        pages, ocr_pages = _fast_with_ocr_fallback(path, ocr_workers, progress)
    chunks: list[Chunk] = []
    for page_no, text in pages:
        for block in _chunk_text(text):
            extra = {"ocr": True} if page_no in ocr_pages else {}
            chunks.append(Chunk(text=block, file=file_name, page=page_no,
                                section=None, extra=extra))
    return chunks
