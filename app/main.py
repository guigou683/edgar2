"""Point d'entrée FastAPI d'EDGAR v2.

Briques actives :
  1. Socle web  : statics vendus localement, /healthz, en-têtes de sécurité.
  2. Auth/sécu  : Argon2id, sessions cookie révocables, rôles, CSRF, audit+IP,
                  politique MDP, anti-bruteforce, inscription (pending -> active).
"""
from __future__ import annotations

import secrets
from typing import Optional

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import auth, db
from core.config import settings
from core.security import (
    CSP_POLICY,
    client_ip,
    make_csrf_token,
    verify_csrf_token,
)

app = FastAPI(title="EDGAR v2", docs_url=None, redoc_url=None)

settings.ensure_dirs()
db.init_db()

app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))  # autoescape ON

CSRF_COOKIE = "edgar_csrf"


# --------------------------------------------------------------------------
# Middleware : en-têtes de sécurité ANSSI
# --------------------------------------------------------------------------
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# --------------------------------------------------------------------------
# Helpers cookies & CSRF
# --------------------------------------------------------------------------
def _set_cookie(response, name: str, value: str, max_age: Optional[int] = None) -> None:
    response.set_cookie(
        key=name, value=value, max_age=max_age, httponly=True,
        samesite="lax", secure=settings.TLS, path="/",
    )


def _csrf_nonce(request: Request) -> str:
    """Nonce CSRF courant (cookie) ou nouveau si absent."""
    return request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(16)


def _render_with_csrf(request: Request, template: str, ctx: dict):
    """Rend un gabarit en injectant un jeton CSRF et en (re)posant le cookie nonce."""
    nonce = _csrf_nonce(request)
    ctx = {**ctx, "request": request, "csrf_token": make_csrf_token(nonce)}
    response = templates.TemplateResponse(template, ctx)
    _set_cookie(response, CSRF_COOKIE, nonce, max_age=3600)
    return response


def _check_csrf(request: Request, token: Optional[str]) -> bool:
    return verify_csrf_token(token, request.cookies.get(CSRF_COOKIE, ""))


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")[:300]


# --------------------------------------------------------------------------
# Pages applicatives
# --------------------------------------------------------------------------
@app.get("/")
async def home(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=303)
    return _render_with_csrf(request, "base.html", {"title": "EDGAR v2", "user": user})


# --------------------------------------------------------------------------
# Authentification
# --------------------------------------------------------------------------
@app.get("/login")
async def login_form(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=303)
    return _render_with_csrf(request, "auth/login.html", {"title": "Connexion"})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    if not _check_csrf(request, csrf_token):
        return _render_with_csrf(request, "auth/login.html",
                                 {"title": "Connexion", "error": "Session expirée, réessayez."})

    ip, ua = client_ip(request), _ua(request)
    user, error = auth.authenticate(username, password)
    if not user:
        db.insert_audit("login_failure", None, username, ip, ua, "échec d'authentification")
        return _render_with_csrf(request, "auth/login.html",
                                 {"title": "Connexion", "error": error})

    sid = auth.create_session(user["id"], ip, ua)
    db.insert_audit("login_success", user["id"], user["username"], ip, ua, "")
    target = "/change-password" if user["must_change_password"] else "/"
    response = RedirectResponse(target, status_code=303)
    _set_cookie(response, auth.SESSION_COOKIE, sid, max_age=auth.SESSION_IDLE_SECONDS)
    return response


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    user = auth.current_user(request)
    sid = request.cookies.get(auth.SESSION_COOKIE)
    if user and _check_csrf(request, csrf_token) and sid:
        auth.logout(sid)
        db.insert_audit("logout", user["id"], user["username"], client_ip(request), _ua(request), "")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------
# Inscription libre (validation administrateur ensuite)
# --------------------------------------------------------------------------
@app.get("/register")
async def register_form(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=303)
    return _render_with_csrf(request, "auth/register.html", {"title": "Inscription"})


# Message unique de confirmation (anti-énumération : identique que le compte existe ou non).
_REGISTER_CONFIRM = ("Demande enregistrée. Un administrateur doit valider votre compte "
                     "avant la première connexion.")


@app.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    confirm: str = Form(...),
    csrf_token: str = Form(...),
):
    if not _check_csrf(request, csrf_token):
        return _render_with_csrf(request, "auth/register.html",
                                 {"title": "Inscription", "error": "Session expirée, réessayez."})

    username = username.strip()
    if not username or password != confirm:
        return _render_with_csrf(request, "auth/register.html",
                                 {"title": "Inscription",
                                  "error": "Vérifiez le nom d'utilisateur et la confirmation."})

    ok, msg = auth.validate_password_policy(password)
    if not ok:
        return _render_with_csrf(request, "auth/register.html",
                                 {"title": "Inscription", "error": msg})

    ip, ua = client_ip(request), _ua(request)
    # Création seulement si le nom est libre, mais réponse identique dans tous les cas.
    if not db.get_user_by_username(username):
        uid = db.create_user(
            username=username, password_hash=auth.hash_password(password),
            role=auth.ROLE_USER, status=auth.STATUS_PENDING,
            email=email.strip() or None, must_change_password=False,
        )
        db.insert_audit("register", uid, username, ip, ua, "compte en attente de validation")
    else:
        db.insert_audit("register_duplicate", None, username, ip, ua, "nom déjà pris")

    return _render_with_csrf(request, "auth/register.html",
                             {"title": "Inscription", "confirm": _REGISTER_CONFIRM})


# --------------------------------------------------------------------------
# Changement de mot de passe (forcé au premier login admin)
# --------------------------------------------------------------------------
@app.get("/change-password")
async def change_password_form(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return _render_with_csrf(request, "auth/change_password.html",
                             {"title": "Changer le mot de passe", "user": user})


@app.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
    csrf_token: str = Form(...),
):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    def again(error: str):
        return _render_with_csrf(request, "auth/change_password.html",
                                 {"title": "Changer le mot de passe", "error": error, "user": user})

    if not _check_csrf(request, csrf_token):
        return again("Session expirée, réessayez.")
    if not auth.verify_password(user["password_hash"], current_password):
        return again("Mot de passe actuel incorrect.")
    if new_password != confirm:
        return again("La confirmation ne correspond pas.")
    ok, msg = auth.validate_password_policy(new_password)
    if not ok:
        return again(msg)
    if new_password == current_password:
        return again("Le nouveau mot de passe doit différer de l'ancien.")

    db.update_password(user["id"], auth.hash_password(new_password), must_change=False)
    db.insert_audit("password_change", user["id"], user["username"],
                    client_ip(request), _ua(request), "")
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Santé
# --------------------------------------------------------------------------
async def _ping(url: str, path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url}{path}")
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


@app.get("/healthz")
async def healthz():
    ollama = await _ping(settings.OLLAMA_URL, "/api/tags")
    qdrant = await _ping(settings.QDRANT_URL, "/healthz")
    healthy = ollama["ok"] and qdrant["ok"]
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"app": "ok", "ollama": ollama, "qdrant": qdrant, "healthy": healthy},
    )
