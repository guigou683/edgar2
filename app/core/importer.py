"""Import de documents en **tâche de fond** avec suivi de progression.

L'ingestion (parsing/OCR/embeddings) est lourde : elle s'exécute dans un thread
dédié pour ne PAS bloquer l'application (fin des blocages / 504). Un registre de
jobs en mémoire expose l'avancement (réussis / ignorés / échecs, fichier courant)
pour un affichage dynamique côté navigateur.
"""
from __future__ import annotations

import multiprocessing
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from core import appsettings, coordinator, db, ingest, vectorstore
from core.config import settings

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def rel_doc_id(base_id: str, path: Path) -> str:
    """Identifiant d'un document = son chemin RELATIF au dossier de la base
    (ex. « PDF-tiny/0001.pdf »), en séparateurs POSIX. Préserve les sous-dossiers
    dans le registre, les liens /doc/... et le payload Qdrant. Repli sur le nom seul
    si le fichier est hors du dossier de la base."""
    try:
        base_dir = (settings.DOCUMENTS_DIR / base_id).resolve()
        return path.resolve().relative_to(base_dir).as_posix()
    except (ValueError, OSError):
        return path.name


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def active_job_for_base(base_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Renvoie le job en cours (status 'running') pour la base donnée, ou le premier
    job en cours toutes bases confondues si base_id est None. Sert à réafficher
    l'encart au retour sur la page et à empêcher un double import."""
    with _lock:
        for j in _jobs.values():
            if j.get("status") == "running" and (base_id is None or j.get("base_id") == base_id):
                return dict(j)
    return None


def record_result(base_id: str, filename: str, rep: dict[str, Any],
                  strategy: str, size: int) -> str:
    """Met à jour le registre des documents selon le résultat d'ingestion.

    Renvoie l'issue : 'indexed' | 'skipped' | 'ignored' | 'failed'.
    """
    if rep.get("ignored"):
        db.upsert_document(base_id, filename, "ignored", 0, 0, None, size,
                           "format non pris en charge")
        return "ignored"
    if rep.get("skipped"):
        return "skipped"
    if rep.get("indexed", 0) > 0:
        # OCR déclenché automatiquement en mode fast (PDF scanné) -> tracé au registre.
        eff_strategy = "fast (OCR auto)" if strategy == "fast" and rep.get("ocr") else strategy
        db.upsert_document(base_id, filename, "indexed", rep["indexed"],
                           rep.get("pages", 0), eff_strategy, size, None)
        return "indexed"
    db.upsert_document(base_id, filename, "failed", 0, 0, strategy, size,
                       (rep.get("reason") or "échec")[:400])
    return "failed"


def request_stop(job_id: str) -> bool:
    """Demande l'arrêt souple d'un job : il s'arrête après le fichier en cours.
    Renvoie True si la demande a été prise en compte (job en cours)."""
    with _lock:
        j = _jobs.get(job_id)
        if not j or j.get("status") != "running":
            return False
        j["stop"] = True
        return True


def _record(job_id: str, base_id: str, doc_id: str, rep: dict, strategy: str, size: int) -> None:
    issue = record_result(base_id, doc_id, rep, strategy, size)
    with _lock:
        j = _jobs[job_id]
        j["done"] += 1
        if issue in ("skipped", "ignored"):
            j["skipped"] += 1
        elif issue == "indexed":
            j["succeeded"] += 1
            j["bytes"] += size  # taille cumulée des documents effectivement ajoutés
        else:
            j["failed"] += 1
            j["failures"].append({"file": doc_id, "reason": rep.get("reason", "")})


def _worker(job_id: str, base_id: str, paths: list[Path], strategy: str,
            reindex: bool) -> None:
    """Pipeline à deux étages : pendant l'embedding GPU du fichier courant, la
    préparation CPU (parsing/OCR/mots-clés/BM25) du fichier suivant est préchargée
    dans un thread → recouvrement CPU↔GPU. `index_workers` (admin) fixe le nombre
    de cœurs pour l'OCR des pages ET l'extraction des mots-clés (pool de processus).
    La progression intra-fichier (étape + compteur) est remontée via `sub`.
    Arrêt souple : stoppe après le fichier en cours.
    """
    workers = appsettings.get_index_workers()

    # Pool de processus pour paralléliser YAKE (pur Python, borné par le GIL en threads).
    # spawn : les workers n'importent que core.keywords (léger), pas le serveur.
    yake_pool = None
    if workers > 1:
        try:
            yake_pool = ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        except Exception:
            yake_pool = None

    def gate() -> None:
        """Avant chaque lot d'embeddings : cède le pas aux requêtes (mode priority)."""
        if coordinator.query_active() and coordinator.policy() == coordinator.POLICY_PRIORITY:
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["paused"] = True
            coordinator.gate()
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["paused"] = False

    _last = [0.0]

    def make_report(doc_id: str):
        def report(stage: str, done: int, total: int) -> None:
            now = time.time()
            final = bool(total) and done >= total
            if now - _last[0] < 0.25 and not final:   # throttle ~4 màj/s
                return
            _last[0] = now
            with _lock:
                j = _jobs.get(job_id)
                if j:
                    j["sub"] = {"file": doc_id, "stage": stage, "done": done, "total": total}
        return report

    def prep_of(path: Path) -> tuple[str, int, dict]:
        doc_id = rel_doc_id(base_id, path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            pf = ingest.prepare_file(base_id, str(path), file_name=doc_id, strategy=strategy,
                                     skip_if_indexed=not reindex, yake_pool=yake_pool,
                                     ocr_workers=workers, progress=make_report(doc_id))
        except Exception as exc:
            pf = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:400]}
        return doc_id, size, pf

    prefetch = ThreadPoolExecutor(max_workers=1)
    stopped = False
    try:
        n = len(paths)
        future = prefetch.submit(prep_of, paths[0]) if n else None
        for i in range(n):
            with _lock:                       # arrêt souple : avant de démarrer un fichier
                if _jobs[job_id].get("stop"):
                    stopped = True
                    break
            doc_id, size, pf = future.result()  # prep CPU (recouverte par l'embed précédent)
            with _lock:
                _jobs[job_id]["current"] = doc_id
            # Précharge la prep CPU du fichier suivant pendant l'embed GPU du courant.
            future = prefetch.submit(prep_of, paths[i + 1]) if i + 1 < n else None

            status = pf.get("status")
            if status == "skipped":
                rep = {"indexed": 0, "skipped": True, "reason": pf.get("reason", "")}
            elif status == "ignored":       # format non pris en charge -> statut « ignored »
                rep = {"indexed": 0, "ignored": True,
                       "reason": pf.get("reason", "format non pris en charge")}
            elif status in ("empty", "error"):
                if reindex:                    # ré-analyse : purge quand même les anciens points
                    try:
                        vectorstore.delete_by_file(base_id, doc_id)
                    except Exception:
                        pass
                rep = {"indexed": 0, "skipped": False, "reason": pf.get("reason", "échec")}
            else:                              # ok -> phase GPU (embeddings + upsert)
                try:
                    rep = ingest.embed_and_upsert(
                        base_id, pf["doc_hash"], pf["chunks"], pf["prep"],
                        delete_file=doc_id if reindex else None,
                        progress=make_report(doc_id), before_batch=gate)
                except Exception as exc:
                    rep = {"indexed": 0, "skipped": False,
                           "reason": f"{type(exc).__name__}: {exc}"[:400]}
            _record(job_id, base_id, doc_id, rep, strategy, size)
    finally:
        prefetch.shutdown(wait=False)
        if yake_pool is not None:
            yake_pool.shutdown(wait=False, cancel_futures=True)
        with _lock:
            _jobs[job_id]["current"] = ""
            _jobs[job_id]["sub"] = None
            _jobs[job_id]["status"] = "stopped" if stopped else "done"
            _jobs[job_id]["finished"] = time.time()


def start(base_id: str, paths: list[str], strategy: str, reindex: bool = False) -> str:
    """Démarre un job d'import en arrière-plan. Renvoie l'identifiant du job."""
    files = [Path(p) for p in paths]
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "id": job_id, "base_id": base_id, "total": len(files), "done": 0,
            "succeeded": 0, "skipped": 0, "failed": 0, "current": "", "bytes": 0,
            "failures": [], "status": "running", "started": time.time(), "stop": False,
            "sub": None, "paused": False,
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
