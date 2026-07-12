"""Coordination indexation <-> requêtes interactives (partage du GPU).

Sur une machine à GPU unique/limité, l'indexation (embeddings bge-m3) et une
requête (embedding de la question + génération) se disputent le GPU et provoquent
des bascules de modèle. Politique par défaut « priority » : l'indexation cède le
pas — elle marque une pause (au niveau du lot d'embeddings) tant qu'une requête
est en cours. Politique « parallel » : aucune pause (machine puissante / Ollama
distant), Ollama gère la file.
"""
from __future__ import annotations

import threading

from core import db

POLICY_PRIORITY = "priority"
POLICY_PARALLEL = "parallel"
POLICIES = (POLICY_PRIORITY, POLICY_PARALLEL)

_lock = threading.Lock()
_active = 0                 # nombre de requêtes interactives en cours
_idle = threading.Event()
_idle.set()                # « set » = aucune requête active


def policy() -> str:
    p = db.get_setting("concurrency_policy")
    return p if p in POLICIES else POLICY_PRIORITY


def query_begin() -> None:
    """À appeler au début d'une requête interactive (chat, résumé)."""
    global _active
    with _lock:
        _active += 1
        _idle.clear()


def query_end() -> None:
    """À appeler à la fin d'une requête interactive (dans un finally)."""
    global _active
    with _lock:
        _active = max(0, _active - 1)
        if _active == 0:
            _idle.set()


def query_active() -> bool:
    return _active > 0


def gate(timeout: float = 15.0) -> bool:
    """Appelé par l'indexation avant une opération GPU. En mode « priority »,
    attend (borné par timeout) qu'aucune requête ne soit active. Renvoie True si
    une pause a effectivement eu lieu."""
    if policy() != POLICY_PRIORITY or _idle.is_set():
        return False
    _idle.wait(timeout)   # borne de sécurité : reprend même si une requête reste bloquée
    return True
