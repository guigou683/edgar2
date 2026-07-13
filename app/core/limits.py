"""Limites de charge : requêtes simultanées (file d'attente) et sessions.

- Requêtes : au plus `max_queries()` questions traitées en parallèle ; au-delà,
  les suivantes attendent leur tour (file d'attente) au lieu de saturer le GPU.
- Sessions : plafond du nombre de sessions actives (contrôlé à la connexion).

Les seuils sont lus à chaud depuis la table settings (réglables dans l'admin).
0 = illimité.
"""
from __future__ import annotations

import threading

from core import db

MAX_QUERIES_DEFAULT = 3
MAX_SESSIONS_DEFAULT = 0   # 0 = illimité


def _int_setting(key: str, default: int) -> int:
    raw = db.get_setting(key)
    try:
        v = int(raw)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def max_queries() -> int:
    return _int_setting("max_queries", MAX_QUERIES_DEFAULT)


def max_sessions() -> int:
    return _int_setting("max_sessions", MAX_SESSIONS_DEFAULT)


# --- File d'attente des requêtes (sémaphore dynamique) ---------------------
_cond = threading.Condition()
_active = 0


def active_queries() -> int:
    return _active


def try_acquire() -> bool:
    """Prend un créneau sans attendre. True si obtenu."""
    global _active
    with _cond:
        limit = max_queries()
        if limit <= 0 or _active < limit:   # 0 = illimité
            _active += 1
            return True
        return False


def acquire() -> None:
    """Prend un créneau, en attendant qu'il s'en libère un (file d'attente)."""
    global _active
    with _cond:
        while True:
            limit = max_queries()
            if limit <= 0 or _active < limit:
                _active += 1
                return
            _cond.wait(timeout=1.0)   # re-vérifie périodiquement (seuil modifiable à chaud)


def release() -> None:
    global _active
    with _cond:
        _active = max(0, _active - 1)
        _cond.notify()
