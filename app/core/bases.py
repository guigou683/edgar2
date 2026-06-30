"""Registre des bases documentaires d'EDGAR v2.

Une base = un dossier de documents + une collection Qdrant dédiés, exposée comme
un chatbot distinct. Cloisonnement total : aucune recherche ne traverse les bases.

Persistance : data/bases.json (écriture atomique). Le dossier de documents de
chaque base vit sous documents/<id> ; sa collection Qdrant est edgar2_<id>.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core import vectorstore
from core.config import settings
from core.security import safe_join

# Stratégies de parsing autorisées (réglables par base).
PARSE_FAST = "fast"
PARSE_OCR = "ocr_only"
PARSE_STRATEGIES = (PARSE_FAST, PARSE_OCR)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    """Transforme un nom en identifiant sûr (ascii kebab-case)."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return name or "base"


# --------------------------------------------------------------------------
# Persistance (lecture / écriture atomique)
# --------------------------------------------------------------------------
def load_bases() -> list[dict[str, Any]]:
    path = settings.bases_json
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_bases(bases: list[dict[str, Any]]) -> None:
    settings.ensure_dirs()
    path = settings.bases_json
    # Écriture atomique : fichier temporaire dans le même dossier puis remplacement.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bases, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get_base(base_id: str) -> Optional[dict[str, Any]]:
    for b in load_bases():
        if b["id"] == base_id:
            return b
    return None


def _unique_id(name: str, existing: set[str]) -> str:
    base = _slugify(name)
    candidate, i = base, 2
    while candidate in existing:
        candidate = f"{base}-{i}"
        i += 1
    return candidate


# --------------------------------------------------------------------------
# Création / mise à jour / suppression
# --------------------------------------------------------------------------
def create_base(name: str, description: str = "", parse_strategy: str = PARSE_FAST,
                llm_enrichment: bool = False) -> dict[str, Any]:
    """Crée une base : entrée de registre + dossier documents + collection Qdrant."""
    name = name.strip()
    if not name:
        raise ValueError("Le nom de la base est obligatoire.")
    if parse_strategy not in PARSE_STRATEGIES:
        raise ValueError(f"Stratégie de parsing inconnue : {parse_strategy}")

    bases = load_bases()
    base_id = _unique_id(name, {b["id"] for b in bases})

    # Dossier de documents cloisonné (anti path-traversal).
    docs_dir = safe_join(settings.DOCUMENTS_DIR, base_id)
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Collection Qdrant dédiée.
    vectorstore.create_collection(base_id)

    base = {
        "id": base_id,
        "name": name,
        "description": description.strip(),
        "collection": vectorstore.collection_name(base_id),
        "docs_dir": str(Path("documents") / base_id),
        "parse_strategy": parse_strategy,
        "llm_enrichment": bool(llm_enrichment),
        "created_at": _now(),
    }
    bases.append(base)
    _save_bases(bases)
    return base


def update_base(base_id: str, **changes: Any) -> Optional[dict[str, Any]]:
    """Met à jour les champs réglables d'une base (description, parsing, enrichissement)."""
    allowed = {"description", "parse_strategy", "llm_enrichment", "name"}
    bases = load_bases()
    updated = None
    for b in bases:
        if b["id"] == base_id:
            for k, v in changes.items():
                if k in allowed:
                    b[k] = v
            updated = b
            break
    if updated:
        _save_bases(bases)
    return updated


def delete_base(base_id: str, remove_documents: bool = False) -> bool:
    """Supprime une base : collection Qdrant + entrée de registre (+ documents en option)."""
    bases = load_bases()
    remaining = [b for b in bases if b["id"] != base_id]
    if len(remaining) == len(bases):
        return False  # introuvable

    vectorstore.delete_collection(base_id)

    if remove_documents:
        docs_dir = safe_join(settings.DOCUMENTS_DIR, base_id)
        if docs_dir.exists():
            import shutil
            shutil.rmtree(docs_dir, ignore_errors=True)

    _save_bases(remaining)
    return True
