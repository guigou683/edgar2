"""Point d'entrée FastAPI d'EDGAR v2 (brique 1 : socle web).

Fournit pour l'instant :
  - le montage des fichiers statiques (tout vendu localement, aucun CDN) ;
  - une page d'accueil minimale (gabarit Jinja2, thème sombre) ;
  - un endpoint /healthz qui vérifie la disponibilité d'Ollama et de Qdrant ;
  - les en-têtes de sécurité de base (CSP stricte, anti-sniffing, etc.).

Les briques suivantes ajouteront : auth/sessions, multi-bases, ingestion,
recherche hybride, reranking, génération en streaming, pages d'administration.
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import settings

app = FastAPI(title="EDGAR v2", docs_url=None, redoc_url=None)

# Prépare les volumes de données (SQLite, bases.json… arriveront plus tard)
settings.ensure_dirs()

# Fichiers statiques servis localement (CSS, JS vendus, polices, logos)
app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))
# autoescape Jinja2 est actif par défaut (anti-XSS)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """En-têtes de sécurité ANSSI appliqués à toutes les réponses.

    CSP : tout en 'self' (assets vendus localement), aucune source externe.
    'unsafe-inline' évité pour les scripts ; toléré pour les styles tant que le
    CSS inline subsiste (sera retiré quand tout passera en fichiers).
    """
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
async def home(request: Request):
    """Page d'accueil (placeholder avant l'intégration du chat complet)."""
    return templates.TemplateResponse("base.html", {"request": request, "title": "EDGAR v2"})


async def _ping(url: str, path: str) -> dict:
    """Interroge un service interne et renvoie son état (sans propager d'exception)."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url}{path}")
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as exc:  # pas de réseau externe : on reste robuste hors-ligne
        return {"ok": False, "error": type(exc).__name__}


@app.get("/healthz")
async def healthz():
    """État de santé : application + dépendances Ollama et Qdrant."""
    ollama = await _ping(settings.OLLAMA_URL, "/api/tags")
    qdrant = await _ping(settings.QDRANT_URL, "/healthz")
    healthy = ollama["ok"] and qdrant["ok"]
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "app": "ok",
            "ollama": ollama,
            "qdrant": qdrant,
            "healthy": healthy,
        },
    )
