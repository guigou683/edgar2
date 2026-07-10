"""Détection des ressources machine et recommandation d'un profil d'indexation.

CPU et RAM sont lus depuis /proc (fiable dans le conteneur). La VRAM du GPU n'est
pas détectable de façon fiable depuis le conteneur applicatif (pas d'accès GPU) :
elle est saisie par l'administrateur (réglage « gpu_vram_gb ») et sert de base au
conseil de modèle de génération.
"""
from __future__ import annotations

import os
from typing import Optional

from core import db

WORKERS_CAP = 8  # borne haute raisonnable pour l'OCR parallèle


def _read_meminfo() -> tuple[int, int]:
    """(RAM totale, RAM disponible) en octets, via /proc/meminfo."""
    total = avail = 0
    try:
        with open("/proc/meminfo", encoding="ascii", errors="ignore") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return total, avail


def vram_gb() -> Optional[float]:
    """VRAM en Go telle que renseignée par l'admin (None si non renseignée)."""
    raw = db.get_setting("gpu_vram_gb")
    if raw:
        try:
            v = float(str(raw).replace(",", "."))
            return v if v > 0 else None
        except (ValueError, TypeError):
            pass
    return None


def _gb(n: int) -> float:
    return round(n / 1073741824, 1)


def detect() -> dict:
    """Ressources détectées + VRAM saisie."""
    cores = os.cpu_count() or 1
    total, avail = _read_meminfo()
    return {
        "cpu_cores": cores,
        "ram_total": total, "ram_total_gb": _gb(total),
        "ram_available": avail, "ram_available_gb": _gb(avail),
        "vram_gb": vram_gb(),
    }


def recommend(res: dict) -> dict:
    """Profil recommandé à partir des ressources détectées."""
    cores = res.get("cpu_cores") or 1
    ram_gb = res.get("ram_total_gb") or 0
    vram = res.get("vram_gb")

    # Workers OCR/CPU : cœurs-1, plafonné par la RAM (~1,5 Go par worker) et borné.
    by_cores = max(1, cores - 1)
    by_ram = max(1, int(ram_gb // 1.5)) if ram_gb else by_cores
    workers = max(1, min(by_cores, by_ram, WORKERS_CAP))

    # Conseil de modèle selon la VRAM (coexistence avec bge-m3 ~2 Go pour éviter la bascule).
    if vram is None:
        model_level, model_advice = "unknown", (
            "VRAM inconnue — renseignez-la ci-dessous pour un conseil de modèle.")
    elif vram >= 12:
        model_level, model_advice = "7b", (
            f"{vram} Go : un modèle 7B (mistral:7b) est confortable et coexiste avec bge-m3.")
    elif vram >= 8:
        model_level, model_advice = "7b-tight", (
            f"{vram} Go : un 7B tient, mais serré avec bge-m3 (bascule de modèle possible).")
    else:
        model_level, model_advice = "3b", (
            f"{vram} Go : préférez un modèle 3B (ex. qwen2.5:3b) qui coexiste avec bge-m3 "
            "et évite la bascule de modèle.")

    return {"index_workers": workers, "model_level": model_level, "model_advice": model_advice}
