"""Réglages de recherche globaux (défauts admin) et fusion avec les surcharges.

Les défauts globaux sont persistés dans la table `settings` (clé JSON). Les
surcharges par requête (issues de l'UI/API) priment sur les défauts globaux,
eux-mêmes prioritaires sur les valeurs codées de SearchParams.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from core import db
from core.retrieval import (
    MODE_BM25,
    MODE_DENSE,
    MODE_HYBRID,
    SearchParams,
)

_KEY = "search_defaults"
_MODES = (MODE_HYBRID, MODE_DENSE, MODE_BM25)

# Champs réglables + coercition/validation associée.
_BOOL = {"use_reprompt", "use_rerank", "search_mode"}
_INT = {"n_reformulations": (1, 5), "top_k": (1, 30), "k_candidates": (1, 100)}
_FLOAT = {"threshold": (0.0, 1.0)}


def _coerce(key: str, value: Any) -> Any:
    """Convertit/valide une valeur ; renvoie None si invalide (ignorée)."""
    try:
        if key == "mode":
            return value if value in _MODES else None
        if key == "llm_model":
            v = str(value).strip()
            return v or None
        if key in _BOOL:
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("1", "true", "on", "yes")
        if key in _INT:
            lo, hi = _INT[key]
            return max(lo, min(hi, int(value)))
        if key in _FLOAT:
            lo, hi = _FLOAT[key]
            return max(lo, min(hi, float(value)))
    except (ValueError, TypeError):
        return None
    return None


_FIELDS = {"mode", "llm_model"} | _BOOL | set(_INT) | set(_FLOAT)


def load_defaults() -> dict[str, Any]:
    raw = db.get_setting(_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def save_defaults(overrides: dict[str, Any]) -> dict[str, Any]:
    """Valide et persiste les défauts globaux ; renvoie le dict enregistré."""
    clean: dict[str, Any] = {}
    for k, v in overrides.items():
        if k in _FIELDS:
            c = _coerce(k, v)
            if c is not None:
                clean[k] = c
    db.set_setting(_KEY, json.dumps(clean, ensure_ascii=False))
    return clean


def build_params(overrides: Optional[dict[str, Any]] = None) -> SearchParams:
    """Construit les paramètres effectifs : défauts codés < défauts globaux < surcharges."""
    p = SearchParams()
    for source in (load_defaults(), overrides or {}):
        for k, v in source.items():
            if k in _FIELDS:
                c = _coerce(k, v)
                if c is not None:
                    setattr(p, k, c)
    return p
