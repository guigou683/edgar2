"""Point d'entrée FastAPI d'EDGAR v2.

Briques actives :
  1. Socle web  : statics vendus localement, /healthz, en-têtes de sécurité.
  2. Auth/sécu  : Argon2id, sessions cookie révocables, rôles, CSRF, audit+IP,
                  politique MDP, anti-bruteforce, inscription (pending -> active).
"""
from __future__ import annotations

import json
import secrets
from typing import Optional

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import auth, bases, db, rag
from core.config import settings
from core.retrieval import SearchParams
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
    return RedirectResponse("/chat", status_code=303)


# --------------------------------------------------------------------------
# Chat (interface + streaming SSE)
# --------------------------------------------------------------------------
_PARAM_FIELDS = ("mode", "use_reprompt", "n_reformulations", "use_rerank",
                 "top_k", "k_candidates", "threshold", "search_mode", "llm_model")


def _params_from(overrides: dict) -> SearchParams:
    """Construit les paramètres de recherche depuis les défauts + surcharges UI."""
    p = SearchParams()
    for k in _PARAM_FIELDS:
        if overrides.get(k) is not None:
            setattr(p, k, overrides[k])
    return p


@app.get("/chat")
async def chat_page(request: Request, base: Optional[str] = None, conv: Optional[int] = None):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=303)

    all_bases = bases.load_bases()
    base_id = base or (all_bases[0]["id"] if all_bases else None)
    convs = db.list_conversations(user["id"], base_id) if base_id else []

    messages = []
    if conv is not None:
        c = db.get_conversation(conv)
        if c and c["user_id"] == user["id"]:
            for m in db.list_messages(conv):
                m["sources"] = json.loads(m["sources_json"]) if m.get("sources_json") else []
                m["diagnostics"] = json.loads(m["diagnostics_json"]) if m.get("diagnostics_json") else None
                messages.append(m)
        else:
            conv = None

    return _render_with_csrf(request, "chat.html", {
        "title": "EDGAR v2", "user": user, "bases": all_bases,
        "base_id": base_id, "conversations": convs, "conv_id": conv,
        "messages": messages,
    })


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    user = auth.current_user(request)
    if not user:
        return JSONResponse({"error": "non authentifié"}, status_code=401)

    body = await request.json()
    if not _check_csrf(request, body.get("csrf_token")):
        return JSONResponse({"error": "CSRF invalide"}, status_code=403)

    base_id = body.get("base_id")
    question = (body.get("question") or "").strip()
    if not base_id or not bases.get_base(base_id):
        return JSONResponse({"error": "base inconnue"}, status_code=404)
    if not question:
        return JSONResponse({"error": "question vide"}, status_code=400)

    # Conversation : création si absente, vérification d'appartenance sinon.
    conv_id = body.get("conversation_id")
    if conv_id:
        c = db.get_conversation(int(conv_id))
        if not c or c["user_id"] != user["id"]:
            return JSONResponse({"error": "conversation introuvable"}, status_code=404)
        conv_id = int(conv_id)
    else:
        conv_id = db.create_conversation(user["id"], base_id, title=question)

    params = _params_from(body.get("params") or {})
    db.add_message(conv_id, "user", question)
    prep = rag.prepare(base_id, question, params)

    def event_stream():
        yield _sse("meta", {
            "conversation_id": conv_id, "found": prep["found"],
            "search_mode": prep["search_mode"], "sources": prep["sources"],
            "diagnostics": prep["diagnostics"],
        })
        parts: list[str] = []
        try:
            if not prep["found"]:
                parts.append(rag.NOT_FOUND_MESSAGE)
                yield _sse("token", {"t": rag.NOT_FOUND_MESSAGE})
            elif not prep["search_mode"]:
                for tok in rag.generate_answer(prep["prompt"], model=params.llm_model):
                    parts.append(tok)
                    yield _sse("token", {"t": tok})
        except Exception as exc:  # robustesse : on signale sans planter le flux
            yield _sse("error", {"message": f"Erreur de génération ({type(exc).__name__})."})

        content = "".join(parts)
        mid = db.add_message(
            conv_id, "assistant", content,
            sources_json=json.dumps(prep["sources"], ensure_ascii=False),
            diagnostics_json=json.dumps(prep["diagnostics"], ensure_ascii=False),
        )
        db.touch_conversation(conv_id)
        yield _sse("done", {"message_id": mid, "conversation_id": conv_id})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
