"""Authentification et autorisation d'EDGAR v2 (conforme ANSSI / PSSI-A).

- Hachage **Argon2id** (sel unique automatique, fonction mémoire-dure).
- Politique de mot de passe : 12–128 caractères, 4 classes.
- Sessions côté serveur (table SQLite), cookie opaque, expiration sur inactivité,
  **révocation immédiate** des droits.
- Anti-bruteforce : verrouillage temporaire après plusieurs échecs.
- Messages d'erreur **génériques** (anti-énumération de comptes).
- Journalisation d'audit horodatée avec IP.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Request

from core import db

# --- Rôles & statuts (valeurs persistées) ---
ROLE_USER = "user"
ROLE_CONTRIBUTOR = "contributor"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_CONTRIBUTOR, ROLE_ADMIN)
ROLE_RANK = {ROLE_USER: 1, ROLE_CONTRIBUTOR: 2, ROLE_ADMIN: 3}

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"

# --- Paramètres de sécurité ---
SESSION_COOKIE = "edgar_session"
SESSION_IDLE_SECONDS = 8 * 3600          # expiration après inactivité
MAX_FAILED_ATTEMPTS = 5                   # avant verrouillage
LOCKOUT_SECONDS = 15 * 60                 # durée du verrouillage

# Message unique présenté à l'utilisateur en cas d'échec de connexion (anti-énumération).
GENERIC_LOGIN_ERROR = "Identifiants invalides ou compte non disponible."

# Argon2id est le type par défaut de PasswordHasher.
_hasher = PasswordHasher()


# --------------------------------------------------------------------------
# Mots de passe
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hache un mot de passe avec Argon2id (sel unique intégré)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Vérifie un mot de passe ; renvoie False sur tout échec/format invalide."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Indique si le hachage doit être recalculé (paramètres Argon2 obsolètes)."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False


_RE_UPPER = re.compile(r"[A-Z]")
_RE_LOWER = re.compile(r"[a-z]")
_RE_DIGIT = re.compile(r"[0-9]")
_RE_SPECIAL = re.compile(r"[^A-Za-z0-9]")


def validate_password_policy(password: str) -> tuple[bool, str]:
    """Applique la politique : longueur 12–128, majuscule, minuscule, chiffre, spécial."""
    if not 12 <= len(password) <= 128:
        return False, "Le mot de passe doit comporter entre 12 et 128 caractères."
    if not _RE_UPPER.search(password):
        return False, "Le mot de passe doit contenir au moins une majuscule."
    if not _RE_LOWER.search(password):
        return False, "Le mot de passe doit contenir au moins une minuscule."
    if not _RE_DIGIT.search(password):
        return False, "Le mot de passe doit contenir au moins un chiffre."
    if not _RE_SPECIAL.search(password):
        return False, "Le mot de passe doit contenir au moins un caractère spécial."
    return True, ""


# --------------------------------------------------------------------------
# Connexion / anti-bruteforce
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(ts) if ts else None


def authenticate(username: str, password: str) -> tuple[Optional[dict], str]:
    """Tente d'authentifier un utilisateur.

    Renvoie (user, "") en cas de succès, ou (None, message_générique) sinon.
    Gère le verrouillage anti-bruteforce et la mise à jour des compteurs.
    Le message reste identique quel que soit le motif (anti-énumération).
    """
    user = db.get_user_by_username(username)
    if not user:
        # Coût constant : on hache quand même pour limiter l'oracle temporel.
        _hasher.hash("timing-equalizer-not-a-real-password")
        return None, GENERIC_LOGIN_ERROR

    locked_until = _parse(user.get("locked_until"))
    if locked_until and locked_until > _now():
        return None, GENERIC_LOGIN_ERROR

    if not verify_password(user["password_hash"], password):
        attempts = int(user["failed_attempts"]) + 1
        lock = None
        if attempts >= MAX_FAILED_ATTEMPTS:
            lock = (_now() + timedelta(seconds=LOCKOUT_SECONDS)).isoformat()
        db.record_login_failure(user["id"], attempts, lock)
        return None, GENERIC_LOGIN_ERROR

    # Seuls les comptes actifs peuvent ouvrir une session (pending/suspended refusés).
    if user["status"] != STATUS_ACTIVE:
        return None, GENERIC_LOGIN_ERROR

    db.reset_login_failures(user["id"])
    # Re-hachage transparent si les paramètres Argon2 ont évolué.
    if needs_rehash(user["password_hash"]):
        db.update_password(user["id"], hash_password(password),
                           must_change=bool(user["must_change_password"]))
    return user, ""


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def create_session(user_id: int, ip: Optional[str], user_agent: Optional[str]) -> str:
    """Crée une session serveur et renvoie le jeton opaque (à poser en cookie)."""
    sid = secrets.token_urlsafe(32)
    expires = (_now() + timedelta(seconds=SESSION_IDLE_SECONDS)).isoformat()
    db.insert_session(sid, user_id, expires, ip, user_agent)
    return sid


def resolve_session(sid: str) -> Optional[dict]:
    """Valide une session et renvoie l'utilisateur associé, ou None.

    Vérifie : session existante, non révoquée, non expirée, compte toujours actif.
    Applique l'expiration glissante (sliding) et invalide si le compte n'est plus actif
    (révocation immédiate des droits).
    """
    sess = db.get_session(sid)
    if not sess or sess["revoked"]:
        return None
    expires = _parse(sess["expires_at"])
    if not expires or expires <= _now():
        return None

    user = db.get_user_by_id(sess["user_id"])
    if not user or user["status"] != STATUS_ACTIVE:
        # Compte supprimé / suspendu / en attente -> session invalidée.
        db.revoke_session(sid)
        return None

    # Expiration glissante (prolongée à chaque requête active).
    new_expires = (_now() + timedelta(seconds=SESSION_IDLE_SECONDS)).isoformat()
    db.touch_session(sid, new_expires)
    return user


def logout(sid: str) -> None:
    db.revoke_session(sid)


# --------------------------------------------------------------------------
# Dépendances FastAPI (injection de l'utilisateur courant)
# --------------------------------------------------------------------------
def current_user(request: Request) -> Optional[dict]:
    """Renvoie l'utilisateur courant (ou None) à partir du cookie de session."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    user = resolve_session(sid)
    if user is not None:
        # Expose l'identifiant de session pour la vérification CSRF des formulaires.
        request.state.session_id = sid
    return user


def has_role(user: Optional[dict], required: str) -> bool:
    """Vrai si l'utilisateur possède au moins le rôle requis (hiérarchique)."""
    if not user:
        return False
    return ROLE_RANK.get(user["role"], 0) >= ROLE_RANK.get(required, 99)
