"""Import de documents en **tâche de fond** avec suivi de progression.

L'ingestion (parsing/OCR/embeddings) est lourde : elle s'exécute dans un thread
dédié pour ne PAS bloquer l'application (fin des blocages / 504). Un registre de
jobs en mémoire expose l'avancement (réussis / ignorés / échecs, fichier courant)
pour un affichage dynamique côté navigateur.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core import db, ingest

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def record_result(base_id: str, filename: str, rep: dict[str, Any],
                  strategy: str, size: int) -> str:
    """Met à jour le registre des documents selon le résultat d'ingestion.

    Renvoie l'issue : 'indexed' | 'skipped' | 'failed'.
    """
    if rep.get("skipped"):
        return "skipped"
    if rep.get("indexed", 0) > 0:
        db.upsert_document(base_id, filename, "indexed", rep["indexed"],
                           rep.get("pages", 0), strategy, size, None)
        return "indexed"
    db.upsert_document(base_id, filename, "failed", 0, 0, strategy, size,
                       (rep.get("reason") or "échec")[:400])
    return "failed"


def _worker(job_id: str, base_id: str, paths: list[Path], strategy: str,
            reindex: bool) -> None:
    for path in paths:
        with _lock:
            _jobs[job_id]["current"] = path.name
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            if reindex:
                rep = ingest.reindex_file(base_id, str(path), file_name=path.name, strategy=strategy)
            else:
                rep = ingest.ingest_file(base_id, str(path), file_name=path.name, strategy=strategy)
        except Exception as exc:  # capture la raison précise de l'échec
            rep = {"indexed": 0, "skipped": False,
                   "reason": f"{type(exc).__name__}: {exc}"[:400]}
        issue = record_result(base_id, path.name, rep, strategy, size)
        with _lock:
            j = _jobs[job_id]
            j["done"] += 1
            if issue == "skipped":
                j["skipped"] += 1
            elif issue == "indexed":
                j["succeeded"] += 1
            else:
                j["failed"] += 1
                j["failures"].append({"file": path.name, "reason": rep.get("reason", "")})
    with _lock:
        _jobs[job_id]["current"] = ""
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["finished"] = time.time()


def start(base_id: str, paths: list[str], strategy: str, reindex: bool = False) -> str:
    """Démarre un job d'import en arrière-plan. Renvoie l'identifiant du job."""
    files = [Path(p) for p in paths]
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "id": job_id, "base_id": base_id, "total": len(files), "done": 0,
            "succeeded": 0, "skipped": 0, "failed": 0, "current": "",
            "failures": [], "status": "running", "started": time.time(),
        }
    threading.Thread(target=_worker, args=(job_id, base_id, files, strategy, reindex),
                     daemon=True).start()
    return job_id


def cleanup(max_age_s: int = 3600) -> None:
    """Purge les jobs terminés anciens (évite l'accumulation en mémoire)."""
    now = time.time()
    with _lock:
        for jid in [k for k, v in _jobs.items()
                    if v.get("status") == "done" and now - v.get("finished", now) > max_age_s]:
            _jobs.pop(jid, None)
