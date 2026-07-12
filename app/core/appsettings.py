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


def build_params(*override_sources: Optional[dict[str, Any]]) -> SearchParams:
    """Paramètres effectifs : défauts codés < défauts globaux < surcharges (dans l'ordre)."""
    p = SearchParams()
    for source in (load_defaults(), *override_sources):
        for k, v in (source or {}).items():
            if k in _FIELDS:
                c = _coerce(k, v)
                if c is not None:
                    setattr(p, k, c)
    return p


# --- Réglages par utilisateur (portée session, priment sur les défauts globaux) ---
def load_user_overrides(user_id: int) -> dict[str, Any]:
    raw = db.get_user_settings(user_id)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def save_user_overrides(user_id: int, overrides: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k, v in overrides.items():
        if k in _FIELDS:
            c = _coerce(k, v)
            if c is not None:
                clean[k] = c
    db.set_user_settings(user_id, json.dumps(clean, ensure_ascii=False))
    return clean


def clear_user_overrides(user_id: int) -> None:
    db.delete_user_settings(user_id)


# --- Parallélisme d'indexation (réglage admin global) ---
_INDEX_KEY = "index_workers"
INDEX_WORKERS_DEFAULT = 2
INDEX_WORKERS_MAX = 16


def get_index_workers() -> int:
    """Nombre de workers pour l'OCR (threads) et l'extraction de mots-clés
    (processus) lors de l'indexation. 1 = séquentiel."""
    try:
        return max(1, min(INDEX_WORKERS_MAX, int(db.get_setting(_INDEX_KEY))))
    except (TypeError, ValueError):
        return INDEX_WORKERS_DEFAULT


def set_index_workers(n: Any) -> int:
    try:
        n = max(1, min(INDEX_WORKERS_MAX, int(n)))
    except (TypeError, ValueError):
        n = INDEX_WORKERS_DEFAULT
    db.set_setting(_INDEX_KEY, str(n))
    return n


# --- Serveur Ollama (URL configurable : Ollama interne ou serveur LAN distant) ---
from core.config import settings as _settings  # noqa: E402

_OLLAMA_KEY = "ollama_url"


def get_ollama_url() -> str:
    """URL Ollama effective : réglage admin s'il est défini, sinon le défaut
    d'environnement (Ollama interne du conteneur)."""
    url = db.get_setting(_OLLAMA_KEY)
    return url.strip().rstrip("/") if url and url.strip() else _settings.OLLAMA_URL


def set_ollama_url(url: Optional[str]) -> str:
    """Enregistre l'URL Ollama. Vide -> revient au défaut interne. Tolère une simple
    « IP:port » (préfixe http:// ajouté). Renvoie l'URL effective."""
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    db.set_setting(_OLLAMA_KEY, url)
    return get_ollama_url()


# --- VRAM du GPU (saisie admin, base des conseils de profil) ---
_VRAM_KEY = "gpu_vram_gb"


def get_concurrency_policy() -> str:
    from core.coordinator import POLICIES, POLICY_PRIORITY
    p = db.get_setting("concurrency_policy")
    return p if p in POLICIES else POLICY_PRIORITY


def set_concurrency_policy(value: Any) -> str:
    from core.coordinator import POLICIES, POLICY_PRIORITY
    v = str(value or "").strip()
    db.set_setting("concurrency_policy", v if v in POLICIES else POLICY_PRIORITY)
    return get_concurrency_policy()


def get_gpu_vram() -> str:
    return db.get_setting(_VRAM_KEY) or ""


def set_gpu_vram(value: Any) -> str:
    """Enregistre la VRAM en Go (>0). Vide/invalide -> effacée."""
    raw = str(value or "").strip().replace(",", ".")
    try:
        v = float(raw)
        db.set_setting(_VRAM_KEY, (str(v) if v > 0 else ""))
    except (ValueError, TypeError):
        db.set_setting(_VRAM_KEY, "")
    return get_gpu_vram()


def effective_dict(user_id: int) -> dict[str, Any]:
    """Valeurs effectives pour initialiser le panneau (globaux < surcharges utilisateur)."""
    p = build_params(load_user_overrides(user_id))
    return {k: getattr(p, k) for k in
            ("mode", "use_reprompt", "n_reformulations", "use_rerank",
             "top_k", "k_candidates", "threshold", "search_mode", "llm_model")}
