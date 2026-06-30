"""Primitives de sécurité transverses d'EDGAR v2.

- Extraction de l'IP cliente (X-Forwarded-For prioritaire derrière proxy).
- Jetons CSRF signés (itsdangerous), liés à la session.
- Assainissement des noms de fichiers à l'upload.
- Confinement des accès fichier (anti path-traversal).
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Optional

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from core.config import settings

# Sérialiseur signé pour les jetons CSRF (clé secrète applicative).
_csrf_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="edgar-csrf")

# Durée de validité d'un jeton CSRF (secondes).
CSRF_MAX_AGE = 3600


def client_ip(request: Request) -> str:
    """IP du client. Derrière un proxy de confiance, le premier maillon de
    X-Forwarded-For prime ; sinon l'IP de la socket.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def make_csrf_token(session_id: str) -> str:
    """Émet un jeton CSRF lié à l'identifiant de session."""
    return _csrf_serializer.dumps(session_id)


def verify_csrf_token(token: Optional[str], session_id: str) -> bool:
    """Vérifie signature, fraîcheur et liaison à la session."""
    if not token:
        return False
    try:
        bound = _csrf_serializer.loads(token, max_age=CSRF_MAX_AGE)
    except BadSignature:
        return False
    return bound == session_id


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str) -> str:
    """Réduit un nom de fichier à un jeu de caractères sûr, sans composante de chemin."""
    # Retire toute composante de répertoire (anti path-traversal sur le nom).
    name = name.replace("\\", "/").split("/")[-1]
    # Normalise les accents -> ASCII.
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = _SAFE_FILENAME.sub("_", name).strip("._")
    return name or "fichier"


def safe_join(base_dir: Path, *paths: str) -> Path:
    """Joint des composants sous base_dir en interdisant toute évasion (path-traversal).

    Lève ValueError si le chemin résolu sort de base_dir.
    """
    base = base_dir.resolve()
    target = base.joinpath(*paths).resolve()
    if base != target and base not in target.parents:
        raise ValueError("Chemin hors du dossier autorisé")
    return target


# --- En-tête CSP partagé (réutilisé par le middleware) ---
CSP_POLICY = (
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
