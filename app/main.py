"""Point d'entrée FastAPI d'EDGAR v2.

Briques actives :
  1. Socle web  : statics vendus localement, /healthz, en-têtes de sécurité.
  2. Auth/sécu  : Argon2id, sessions cookie révocables, rôles, CSRF, audit+IP,
                  politique MDP, anti-bruteforce, inscription (pending -> active).
"""
from __future__ import annotations

import csv
import io
import json
import secrets
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import appsettings, auth, bases, db, ingest, rag, vectorstore
from core.config import settings
from core.security import (
    CSP_POLICY,
    client_ip,
    make_csrf_token,
    safe_join,
    sanitize_filename,
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
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/doc/"):
        # Documents servis : consultables en iframe même-origine (aperçu PDF).
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'self'"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["Content-Security-Policy"] = CSP_POLICY
        response.headers["X-Frame-Options"] = "DENY"
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
        # Panneau d'expérimentation : valeurs effectives + modèles de génération.
        "panel": appsettings.effective_dict(user["id"]),
        "is_admin": auth.has_role(user, auth.ROLE_ADMIN),
        "generation_models": await _generation_models(),
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

    # Fusion : défauts globaux < réglages utilisateur < surcharges de la requête.
    overrides = body.get("params") or {}
    user_overrides = appsettings.load_user_overrides(user["id"])
    # Sécurité : seul un admin peut imposer le modèle de génération (chargement VRAM).
    if not auth.has_role(user, auth.ROLE_ADMIN):
        overrides = {k: v for k, v in overrides.items() if k != "llm_model"}
        user_overrides = {k: v for k, v in user_overrides.items() if k != "llm_model"}
    params = appsettings.build_params(user_overrides, overrides)
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
    email = email.strip()
    if not username or password != confirm:
        return _render_with_csrf(request, "auth/register.html",
                                 {"title": "Inscription",
                                  "error": "Vérifiez le nom d'utilisateur et la confirmation."})
    if "@" not in email or "." not in email.split("@")[-1]:
        return _render_with_csrf(request, "auth/register.html",
                                 {"title": "Inscription",
                                  "error": "Un courriel valide est obligatoire."})

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
# Réglages de recherche par session/utilisateur (panneau d'expérimentation)
# --------------------------------------------------------------------------
@app.post("/api/settings")
async def save_settings(request: Request):
    user = auth.current_user(request)
    if not user:
        return JSONResponse({"error": "non authentifié"}, status_code=401)
    body = await request.json()
    if not _check_csrf(request, body.get("csrf_token")):
        return JSONResponse({"error": "CSRF invalide"}, status_code=403)
    params = body.get("params") or {}
    # Un non-admin ne peut pas fixer le modèle de génération.
    if not auth.has_role(user, auth.ROLE_ADMIN):
        params = {k: v for k, v in params.items() if k != "llm_model"}
    saved = appsettings.save_user_overrides(user["id"], params)
    return JSONResponse({"ok": True, "params": saved})


@app.post("/api/settings/reset")
async def reset_settings(request: Request):
    user = auth.current_user(request)
    if not user:
        return JSONResponse({"error": "non authentifié"}, status_code=401)
    body = await request.json()
    if not _check_csrf(request, body.get("csrf_token")):
        return JSONResponse({"error": "CSRF invalide"}, status_code=403)
    appsettings.clear_user_overrides(user["id"])
    return JSONResponse({"ok": True, "params": appsettings.effective_dict(user["id"])})


# --------------------------------------------------------------------------
# Suppression de conversation
# --------------------------------------------------------------------------
@app.post("/conversations/{conv_id}/delete")
async def conversation_delete(request: Request, conv_id: int,
                              csrf_token: str = Form(...), base: str = Form("")):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    c = db.get_conversation(conv_id)
    if _check_csrf(request, csrf_token) and c and c["user_id"] == user["id"]:
        db.delete_conversation(conv_id)
    dest = f"/chat?base={base}" if base else "/chat"
    return RedirectResponse(dest, status_code=303)


# --------------------------------------------------------------------------
# Consultation de document (aperçu PDF positionné, téléchargement) — confiné
# --------------------------------------------------------------------------
@app.get("/doc/{base_id}/{filename:path}")
async def serve_doc(request: Request, base_id: str, filename: str,
                    download: int = 0):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not bases.get_base(base_id):
        return JSONResponse({"error": "base inconnue"}, status_code=404)
    try:
        # Confinement strict au dossier de la base (anti path-traversal).
        path = safe_join(settings.DOCUMENTS_DIR, base_id, filename)
    except ValueError:
        return JSONResponse({"error": "chemin invalide"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"error": "fichier introuvable"}, status_code=404)
    disposition = "attachment" if download else "inline"
    return FileResponse(str(path), filename=path.name,
                        content_disposition_type=disposition)


# --------------------------------------------------------------------------
# Administration (selon rôle) & contribution
# --------------------------------------------------------------------------
def _guard(request: Request, role: str):
    """Contrôle d'accès. Renvoie (user, None) si autorisé, sinon (None, réponse)."""
    user = auth.current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user["must_change_password"]:
        return None, RedirectResponse("/change-password", status_code=303)
    if not auth.has_role(user, role):
        resp = _render_with_csrf(request, "admin/denied.html",
                                 {"user": user, "title": "Accès refusé"})
        resp.status_code = 403
        return None, resp
    return user, None


@app.get("/admin")
async def admin_dashboard(request: Request):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    ollama = await _ping(settings.OLLAMA_URL, "/api/tags")
    qdrant = await _ping(settings.QDRANT_URL, "/healthz")
    base_stats = []
    for b in bases.load_bases():
        try:
            n = vectorstore.count_points(b["id"])
        except Exception:
            n = None
        base_stats.append({**b, "points": n})
    return _render_with_csrf(request, "admin/dashboard.html", {
        "user": user, "title": "Administration", "ollama": ollama,
        "qdrant": qdrant, "base_stats": base_stats,
    })


@app.get("/admin/bases")
async def admin_bases(request: Request):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    return _render_with_csrf(request, "admin/bases.html", {
        "user": user, "title": "Bases documentaires", "bases": bases.load_bases(),
        "strategies": bases.PARSE_STRATEGIES,
    })


@app.post("/admin/bases/create")
async def admin_bases_create(
    request: Request, name: str = Form(...), description: str = Form(""),
    parse_strategy: str = Form("fast"), llm_enrichment: str = Form(""),
    csrf_token: str = Form(...),
):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    if _check_csrf(request, csrf_token) and name.strip():
        try:
            bases.create_base(name, description, parse_strategy, llm_enrichment == "on")
            db.insert_audit("base_create", user["id"], user["username"],
                            client_ip(request), _ua(request), name.strip())
        except Exception:
            pass
    return RedirectResponse("/admin/bases", status_code=303)


@app.post("/admin/bases/delete")
async def admin_bases_delete(request: Request, base_id: str = Form(...),
                             csrf_token: str = Form(...)):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    if _check_csrf(request, csrf_token):
        bases.delete_base(base_id, remove_documents=True)
        db.insert_audit("base_delete", user["id"], user["username"],
                        client_ip(request), _ua(request), base_id)
    return RedirectResponse("/admin/bases", status_code=303)


@app.get("/admin/users")
async def admin_users(request: Request):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    return _render_with_csrf(request, "admin/users.html", {
        "user": user, "title": "Comptes", "users": db.list_users(), "roles": auth.ROLES,
    })


@app.post("/admin/users/action")
async def admin_users_action(request: Request, user_id: int = Form(...),
                             action: str = Form(...), role: str = Form(""),
                             csrf_token: str = Form(...)):
    admin, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp

    def users_page(error: str = ""):
        return _render_with_csrf(request, "admin/users.html", {
            "user": admin, "title": "Comptes", "users": db.list_users(),
            "roles": auth.ROLES, "error": error})

    if not _check_csrf(request, csrf_token):
        return users_page("Session expirée, réessayez.")
    target = db.get_user_by_id(user_id)
    if not target:
        return RedirectResponse("/admin/users", status_code=303)

    # Anti-verrouillage : un admin ne peut ni se suspendre ni se rétrograder lui-même.
    if user_id == admin["id"] and action in ("suspend", "set_role"):
        return users_page("Vous ne pouvez pas modifier votre propre rôle ni votre statut "
                          "(sécurité anti-verrouillage).")

    if action == "activate":
        db.set_status(user_id, auth.STATUS_ACTIVE)
    elif action == "suspend":
        db.set_status(user_id, auth.STATUS_SUSPENDED)
        db.revoke_user_sessions(user_id)              # révocation immédiate (cible)
    elif action == "set_role" and role in auth.ROLES:
        db.set_role(user_id, role)
        db.revoke_user_sessions(user_id)              # rôle changé -> re-login (cible)
    db.insert_audit(f"user_{action}", admin["id"], admin["username"],
                    client_ip(request), _ua(request),
                    f"cible={target['username']} role={role}")
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/create")
async def admin_users_create(request: Request, username: str = Form(...),
                             password: str = Form(...), role: str = Form("user"),
                             email: str = Form(""), must_change: str = Form(""),
                             csrf_token: str = Form(...)):
    admin, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp

    def users_page(error: str = "", ok: str = ""):
        return _render_with_csrf(request, "admin/users.html", {
            "user": admin, "title": "Comptes", "users": db.list_users(),
            "roles": auth.ROLES, "error": error, "created": ok})

    if not _check_csrf(request, csrf_token):
        return users_page("Session expirée, réessayez.")
    username = username.strip()
    if not username or role not in auth.ROLES:
        return users_page("Nom d'utilisateur ou rôle invalide.")
    if db.get_user_by_username(username):
        return users_page("Ce nom d'utilisateur est déjà pris.")
    ok, msg = auth.validate_password_policy(password)
    if not ok:
        return users_page(msg)
    uid = db.create_user(username=username, password_hash=auth.hash_password(password),
                         role=role, status=auth.STATUS_ACTIVE,
                         email=email.strip() or None,
                         must_change_password=(must_change == "on"))
    db.insert_audit("user_create", admin["id"], admin["username"],
                    client_ip(request), _ua(request), f"cible={username} role={role}")
    return users_page(ok=f"Compte « {username} » créé ({role}).")


@app.get("/admin/models")
async def admin_models(request: Request):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    return _render_with_csrf(request, "admin/models.html", {
        "user": user, "title": "Modèles & recherche",
        "installed": await _installed_models(),
        "generation_models": await _generation_models(),
        "defaults": appsettings.load_defaults(),
    })


@app.post("/admin/models/save")
async def admin_models_save(request: Request):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    form = await request.form()
    if _check_csrf(request, form.get("csrf_token")):
        overrides = {
            "mode": form.get("mode"),
            "llm_model": form.get("llm_model"),
            "use_reprompt": form.get("use_reprompt") == "on",
            "n_reformulations": form.get("n_reformulations"),
            "use_rerank": form.get("use_rerank") == "on",
            "top_k": form.get("top_k"),
            "k_candidates": form.get("k_candidates"),
            "threshold": form.get("threshold"),
        }
        appsettings.save_defaults(overrides)
        db.insert_audit("settings_update", user["id"], user["username"],
                        client_ip(request), _ua(request), "")
    return RedirectResponse("/admin/models", status_code=303)


@app.get("/admin/logs")
async def admin_logs(request: Request):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    return _render_with_csrf(request, "admin/logs.html", {
        "user": user, "title": "Journaux", "entries": db.list_audit(200),
    })


@app.get("/admin/logs/export.csv")
async def admin_logs_csv(request: Request):
    user, resp = _guard(request, auth.ROLE_ADMIN)
    if resp:
        return resp
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["horodatage_utc", "action", "utilisateur", "ip", "user_agent", "detail"])
    for e in db.list_audit(100000):
        writer.writerow([e.get("ts", ""), e.get("action", ""), e.get("username") or "",
                         e.get("ip") or "", e.get("user_agent") or "", e.get("detail") or ""])
    csv_bytes = "﻿" + buf.getvalue()  # BOM pour Excel (accents corrects)
    return StreamingResponse(
        iter([csv_bytes]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="edgar_audit.csv"'})


@app.get("/contribute")
async def contribute_page(request: Request):
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    return _render_with_csrf(request, "admin/contribute.html", {
        "user": user, "title": "Importer des documents", "bases": bases.load_bases(),
    })


def _contribute_result(request, user, result):
    return _render_with_csrf(request, "admin/contribute.html", {
        "user": user, "title": "Importer des documents",
        "bases": bases.load_bases(), "result": result})


@app.post("/contribute/upload")
async def contribute_upload(request: Request, base_id: str = Form(...),
                            csrf_token: str = Form(...),
                            files: list[UploadFile] = File(...)):
    """Import d'un ou plusieurs fichiers depuis le navigateur."""
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    base = bases.get_base(base_id)
    if not _check_csrf(request, csrf_token):
        return _contribute_result(request, user, {"ok": False, "message": "Session expirée, réessayez."})
    if not base:
        return _contribute_result(request, user, {"ok": False, "message": "Base inconnue."})

    docs_dir = safe_join(settings.DOCUMENTS_DIR, base_id)
    docs_dir.mkdir(parents=True, exist_ok=True)
    strategy = base.get("parse_strategy", "fast")
    indexed_docs, skipped, errors, total_chunks = 0, 0, 0, 0
    for f in files:
        if not f.filename:
            continue
        safe_name = sanitize_filename(f.filename)
        try:
            dest = safe_join(docs_dir, safe_name)
            dest.write_bytes(await f.read())
            rep = ingest.ingest_file(base_id, str(dest), file_name=safe_name, strategy=strategy)
            if rep.get("skipped"):
                skipped += 1
            else:
                indexed_docs += 1
                total_chunks += rep.get("indexed", 0)
            db.insert_audit("document_upload", user["id"], user["username"],
                            client_ip(request), _ua(request),
                            f"base={base_id} fichier={safe_name} {rep}")
        except Exception:
            errors += 1
    msg = (f"{indexed_docs} document(s) importé(s) ({total_chunks} extraits), "
           f"{skipped} déjà indexé(s)" + (f", {errors} en échec." if errors else "."))
    return _contribute_result(request, user, {"ok": errors == 0, "message": msg})


@app.post("/contribute/scan")
async def contribute_scan(request: Request, base_id: str = Form(...),
                          csrf_token: str = Form(...)):
    """Ingestion de tout le dossier serveur de la base (fichiers non encore indexés)."""
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    base = bases.get_base(base_id)
    if not _check_csrf(request, csrf_token):
        return _contribute_result(request, user, {"ok": False, "message": "Session expirée, réessayez."})
    if not base:
        return _contribute_result(request, user, {"ok": False, "message": "Base inconnue."})

    docs_dir = safe_join(settings.DOCUMENTS_DIR, base_id)
    if not docs_dir.exists():
        return _contribute_result(request, user, {"ok": False, "message": "Dossier de la base introuvable."})
    strategy = base.get("parse_strategy", "fast")
    indexed_docs, skipped, errors, total_chunks = 0, 0, 0, 0
    for path in sorted(docs_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            rep = ingest.ingest_file(base_id, str(path), file_name=path.name, strategy=strategy)
            if rep.get("skipped"):
                skipped += 1
            else:
                indexed_docs += 1
                total_chunks += rep.get("indexed", 0)
        except Exception:
            errors += 1
    db.insert_audit("folder_scan", user["id"], user["username"],
                    client_ip(request), _ua(request),
                    f"base={base_id} indexés={indexed_docs} ignorés={skipped} erreurs={errors}")
    msg = (f"Scan terminé : {indexed_docs} nouveau(x) document(s) ({total_chunks} extraits), "
           f"{skipped} déjà indexé(s)" + (f", {errors} en échec." if errors else "."))
    return _contribute_result(request, user, {"ok": errors == 0, "message": msg})


# --------------------------------------------------------------------------
# Santé
# --------------------------------------------------------------------------
async def _installed_models() -> list[str]:
    """Liste des modèles Ollama installés."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.OLLAMA_URL}/api/tags")
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


# Indices de nom permettant d'exclure les modèles non génératifs (embeddings/rerankers).
_NON_GEN_HINTS = ("embed", "bge-m3", "bge-large", "reranker", "rerank", "minilm", "e5-")


async def _generation_models() -> list[str]:
    """Modèles de génération (LLM) uniquement — exclut embeddings et rerankers."""
    return [m for m in await _installed_models()
            if not any(h in m.lower() for h in _NON_GEN_HINTS)]


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
