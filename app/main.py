"""Point d'entrée FastAPI d'EDGAR v2.

Briques actives :
  1. Socle web  : statics vendus localement, /healthz, en-têtes de sécurité.
  2. Auth/sécu  : Argon2id, sessions cookie révocables, rôles, CSRF, audit+IP,
                  politique MDP, anti-bruteforce, inscription (pending -> active).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import time
from pathlib import Path
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
from starlette.concurrency import run_in_threadpool

from core import appsettings, auth, bases, db, importer, ingest, rag, retrieval, vectorstore
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
# Version d'asset (change à chaque démarrage) -> anti-cache navigateur pour CSS/JS.
templates.env.globals["asset_v"] = str(int(time.time()))

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
@app.get("/about")
async def about_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return _render_with_csrf(request, "about.html",
                             {"user": user, "title": "À propos", "version": settings.VERSION})


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

    # Le pipeline (lourd) s'exécute dans le générateur sync -> itéré en threadpool
    # par Starlette : la boucle asynchrone n'est pas bloquée. Chaque étape est
    # diffusée en direct (affichage type « recherche »), puis la génération.
    def event_stream():
        out = {"results": [], "diagnostics": {}}
        try:
            for ev in retrieval.retrieve_iter(base_id, question, params):
                if ev[0] == "step":
                    yield _sse("step", {"label": ev[1]})
                elif ev[0] == "result":
                    out = ev[1]
        except Exception as exc:
            yield _sse("error", {"message": f"Erreur de recherche ({type(exc).__name__})."})

        prep = rag.assemble(question, out, params)
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
                yield _sse("step", {"label": "Génération de la réponse…"})
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


@app.post("/conversations/delete-all")
async def conversations_delete_all(request: Request, base: str = Form(...),
                                   csrf_token: str = Form(...)):
    """Supprime toutes les conversations de l'utilisateur courant pour une base."""
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if _check_csrf(request, csrf_token) and base:
        n = db.delete_conversations_for_base(user["id"], base)
        db.insert_audit("conversations_clear", user["id"], user["username"],
                        client_ip(request), _ua(request), f"base={base} n={n}")
    return RedirectResponse(f"/chat?base={base}", status_code=303)


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
# Recherche & gestion des documents
# --------------------------------------------------------------------------
def _reconcile_documents(base_id: str) -> None:
    """Renseigne le registre pour les fichiers présents sur disque mais absents
    de la table (ex. documents indexés avant cette version)."""
    from qdrant_client import models as qm
    try:
        docs_dir = safe_join(settings.DOCUMENTS_DIR, base_id)
    except ValueError:
        return
    if not docs_dir.exists():
        return
    known = {d["filename"] for d in db.list_documents(base_id)}
    client = vectorstore.get_client()
    coll = vectorstore.collection_name(base_id)
    exists = client.collection_exists(coll)
    for p in sorted(docs_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(docs_dir).as_posix()  # préserve les sous-dossiers
        # rel déjà connu ; ou entrée héritée sous le seul nom (évite un doublon).
        if rel in known or p.name in known:
            continue
        cnt = 0
        if exists:
            try:
                cnt = client.count(coll, exact=True, count_filter=qm.Filter(must=[
                    qm.FieldCondition(key="file", match=qm.MatchValue(value=rel))])).count
            except Exception:
                cnt = 0
        db.upsert_document(base_id, rel, "indexed" if cnt else "pending",
                           cnt, 0, None, p.stat().st_size, None)


@app.get("/documents")
async def documents_page(request: Request, base: Optional[str] = None,
                         q: str = "", status: str = ""):
    """Recherche/consultation de documents — accessible à tous les rôles."""
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=303)
    all_bases = bases.load_bases()
    base_id = base or (all_bases[0]["id"] if all_bases else None)
    docs, stats = [], {}
    if base_id and bases.get_base(base_id):
        await run_in_threadpool(_reconcile_documents, base_id)
        docs = db.list_documents(base_id)          # tout : filtrage live côté client
        stats = db.count_documents(base_id)
    overview = _documents_overview(docs)
    tree = _documents_tree(docs)
    return _render_with_csrf(request, "documents.html", {
        "user": user, "title": "Documents", "bases": all_bases, "base_id": base_id,
        "documents": docs, "stats": stats, "overview": overview, "tree": tree,
        "q": q, "status": status,
        "can_manage": auth.has_role(user, auth.ROLE_CONTRIBUTOR),
    })


def _documents_tree(docs: list[dict]) -> dict:
    """Construit un arbre de dossiers/fichiers à partir des chemins relatifs des
    documents (« sous-dossier/fichier.pdf »). Chaque nœud porte son total récursif."""
    root: dict = {"dirs": {}, "files": [], "count": 0}
    for d in docs:
        parts = (d.get("filename") or "").split("/")
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, {"dirs": {}, "files": [], "count": 0})
        node["files"].append(d)

    def _count(n: dict) -> int:
        total = len(n["files"]) + sum(_count(c) for c in n["dirs"].values())
        n["count"] = total
        return total

    _count(root)
    return root


def _human_size(n: int) -> str:
    for unit, div in (("Go", 1073741824), ("Mo", 1048576), ("Ko", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} o"


def _documents_overview(docs: list[dict]) -> dict:
    """Agrège les statistiques d'une base : totaux + répartitions (stratégie, statut)."""
    from collections import Counter
    by_strategy = Counter((d.get("strategy") or "—") for d in docs)
    by_status = Counter((d.get("status") or "?") for d in docs)
    size = sum(d.get("size") or 0 for d in docs)
    return {
        "files": len(docs),
        "chunks": sum(d.get("chunks") or 0 for d in docs),
        "pages": sum(d.get("pages") or 0 for d in docs),
        "size": size,
        "size_h": _human_size(size),
        "by_strategy": dict(by_strategy),
        "by_status": dict(by_status),
    }


@app.get("/documents/summary")
async def document_summary(request: Request, base: str, file: str, force: int = 0):
    """Flux SSE : synthèse d'un document (LLM) à partir de tous ses extraits indexés.

    Le résumé est mis en cache (table document_summaries) : au premier appel il est
    généré puis enregistré ; les appels suivants le renvoient instantanément. Le
    bouton « Régénérer » passe force=1 pour forcer une nouvelle génération."""
    user = auth.current_user(request)
    if not user:
        return JSONResponse({"error": "authentification requise"}, status_code=401)
    if not bases.get_base(base):
        return JSONResponse({"error": "base inconnue"}, status_code=404)
    name = file.strip().replace("\\", "/")

    # Cache : renvoi immédiat si un résumé existe et qu'on ne force pas la régénération.
    if not force:
        cached = db.get_document_summary(base, name)
        if cached:
            def gen_cached():
                yield _sse("meta", {"chunks": cached.get("chunks", 0), "truncated": False,
                                    "cached": True, "created_at": cached.get("created_at")})
                yield _sse("token", {"t": cached["summary"]})
                yield _sse("done", {"cached": True})
            return StreamingResponse(gen_cached(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    payloads = await run_in_threadpool(vectorstore.chunks_for_file, base, name)

    def gen():
        if not payloads:
            yield _sse("error", {"message": "Aucun extrait indexé pour ce document."})
            yield _sse("done", {})
            return
        _, truncated = rag.build_summary_prompt(name, payloads)
        yield _sse("meta", {"chunks": len(payloads), "truncated": truncated, "cached": False})
        parts = []
        try:
            for tok in rag.summarize_stream(name, payloads):
                parts.append(tok)
                yield _sse("token", {"t": tok})
        except Exception as exc:
            yield _sse("error", {"message": f"Erreur de génération ({type(exc).__name__})."})
            yield _sse("done", {})
            return
        summary = "".join(parts).strip()
        if summary:  # enregistrement pour réutilisation (évite de recalculer)
            db.save_document_summary(base, name, summary, settings.LLM_MODEL,
                                     len(payloads), user["username"])
        yield _sse("done", {"saved": bool(summary)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/documents/delete")
async def documents_delete(request: Request, base_id: str = Form(...),
                           filename: str = Form(...), csrf_token: str = Form(...)):
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    if _check_csrf(request, csrf_token) and bases.get_base(base_id):
        # Identifiant relatif (sous-dossiers conservés) ; safe_join bloque toute évasion.
        name = filename.strip().replace("\\", "/")
        vectorstore.delete_by_file(base_id, name)
        try:
            path = safe_join(settings.DOCUMENTS_DIR, base_id, name)
            if path.is_file():
                path.unlink()
        except ValueError:
            pass
        db.delete_document_row(base_id, name)
        db.insert_audit("document_delete", user["id"], user["username"],
                        client_ip(request), _ua(request), f"base={base_id} fichier={name}")
    return RedirectResponse(f"/documents?base={base_id}", status_code=303)


@app.post("/documents/reanalyze")
async def documents_reanalyze(request: Request, base_id: str = Form(...),
                              filename: str = Form(...), strategy: str = Form("auto"),
                              csrf_token: str = Form(...)):
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    base = bases.get_base(base_id)
    if _check_csrf(request, csrf_token) and base:
        try:
            path = safe_join(settings.DOCUMENTS_DIR, base_id,
                             filename.strip().replace("\\", "/"))
        except ValueError:
            path = None
        if path and path.is_file():
            strat = _resolve_strategy(base, strategy)
            # Le contenu va changer : le résumé mis en cache devient caduc.
            db.delete_document_summary(base_id, filename.strip().replace("\\", "/"))
            job_id = importer.start(base_id, [str(path)], strat, reindex=True)
            db.insert_audit("document_reanalyze", user["id"], user["username"],
                            client_ip(request), _ua(request),
                            f"base={base_id} fichier={path.name} stratégie={strat}")
            return JSONResponse({"job_id": job_id, "count": 1})
    return JSONResponse({"error": "document introuvable"}, status_code=404)


@app.post("/documents/retry-failed")
async def documents_retry_failed(request: Request, base_id: str = Form(...),
                                 strategy: str = Form("ocr_only"), csrf_token: str = Form(...)):
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    base = bases.get_base(base_id)
    if not (_check_csrf(request, csrf_token) and base):
        return JSONResponse({"error": "requête invalide"}, status_code=400)
    paths = []
    for d in db.list_documents(base_id, status="failed"):
        try:
            p = safe_join(settings.DOCUMENTS_DIR, base_id, d["filename"])
            if p.is_file():
                paths.append(str(p))
        except ValueError:
            pass
    if not paths:
        return JSONResponse({"error": "aucun document en échec"}, status_code=400)
    strat = _resolve_strategy(base, strategy)
    job_id = importer.start(base_id, paths, strat, reindex=True)
    db.insert_audit("retry_failed", user["id"], user["username"],
                    client_ip(request), _ua(request), f"base={base_id} n={len(paths)}")
    return JSONResponse({"job_id": job_id, "count": len(paths)})


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
    runtime = await _ollama_runtime()
    base_stats = []
    for b in bases.load_bases():
        try:
            n = vectorstore.count_points(b["id"])
        except Exception:
            n = None
        base_stats.append({**b, "points": n})
    return _render_with_csrf(request, "admin/dashboard.html", {
        "user": user, "title": "Administration", "ollama": ollama,
        "qdrant": qdrant, "runtime": runtime, "base_stats": base_stats,
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
        "strategies": _STRATEGY_CHOICES,
    })


_STRATEGY_CHOICES = ("auto", "fast", "ocr_only")


def _resolve_strategy(base: dict, requested: str) -> str:
    """'auto' -> stratégie de la base ; sinon la stratégie demandée (fast/ocr_only)."""
    return requested if requested in ("fast", "ocr_only") else base.get("parse_strategy", "fast")


@app.post("/contribute/upload")
async def contribute_upload(request: Request, base_id: str = Form(...),
                            csrf_token: str = Form(...), strategy: str = Form("auto"),
                            files: list[UploadFile] = File(...)):
    """Enregistre les fichiers et lance l'import en tâche de fond (renvoie job_id)."""
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    if not _check_csrf(request, csrf_token):
        return JSONResponse({"error": "CSRF invalide"}, status_code=403)
    base = bases.get_base(base_id)
    if not base:
        return JSONResponse({"error": "base inconnue"}, status_code=404)

    docs_dir = safe_join(settings.DOCUMENTS_DIR, base_id)
    docs_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if not f.filename:
            continue
        name = sanitize_filename(f.filename)
        dest = safe_join(docs_dir, name)
        dest.write_bytes(await f.read())     # I/O async, ne bloque pas la boucle
        saved.append(str(dest))
    if not saved:
        return JSONResponse({"error": "aucun fichier"}, status_code=400)

    strat = _resolve_strategy(base, strategy)
    job_id = importer.start(base_id, saved, strat)
    db.insert_audit("document_upload", user["id"], user["username"],
                    client_ip(request), _ua(request),
                    f"base={base_id} fichiers={len(saved)} stratégie={strat}")
    return JSONResponse({"job_id": job_id, "count": len(saved)})


@app.post("/contribute/scan")
async def contribute_scan(request: Request, base_id: str = Form(...),
                          csrf_token: str = Form(...), strategy: str = Form("auto")):
    """Lance en tâche de fond l'ingestion de tout le dossier serveur de la base."""
    user, resp = _guard(request, auth.ROLE_CONTRIBUTOR)
    if resp:
        return resp
    if not _check_csrf(request, csrf_token):
        return JSONResponse({"error": "CSRF invalide"}, status_code=403)
    base = bases.get_base(base_id)
    if not base:
        return JSONResponse({"error": "base inconnue"}, status_code=404)
    docs_dir = safe_join(settings.DOCUMENTS_DIR, base_id)
    if not docs_dir.exists():
        return JSONResponse({"error": "dossier introuvable"}, status_code=404)
    paths = [str(p) for p in sorted(docs_dir.rglob("*")) if p.is_file()]
    if not paths:
        return JSONResponse({"error": "dossier vide"}, status_code=400)

    strat = _resolve_strategy(base, strategy)
    job_id = importer.start(base_id, paths, strat)
    db.insert_audit("folder_scan", user["id"], user["username"],
                    client_ip(request), _ua(request), f"base={base_id} fichiers={len(paths)}")
    return JSONResponse({"job_id": job_id, "count": len(paths)})


@app.get("/contribute/jobs/{job_id}/stream")
async def contribute_job_stream(request: Request, job_id: str):
    """Flux SSE de progression d'un job d'import (affichage dynamique)."""
    if not auth.current_user(request):
        return JSONResponse({"error": "non authentifié"}, status_code=401)

    async def gen():
        importer.cleanup()
        while True:
            job = importer.get_job(job_id)
            if not job:
                yield _sse("error", {"message": "job introuvable"})
                return
            yield _sse("progress", job)
            if job.get("status") == "done":
                yield _sse("done", job)
                return
            await asyncio.sleep(0.7)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


async def _ollama_runtime() -> dict:
    """État d'exécution Ollama : modèles chargés, GPU/CPU, VRAM allouée (/api/ps)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.OLLAMA_URL}/api/ps")
        out = []
        for m in r.json().get("models", []):
            size = int(m.get("size", 0) or 0)
            vram = int(m.get("size_vram", 0) or 0)
            where = "GPU" if size and vram >= size else ("CPU" if vram == 0 else "GPU + CPU")
            out.append({"name": m.get("name", "?"), "size": size, "vram": vram,
                        "pct": round(vram / size * 100) if size else 0, "where": where})
        return {"ok": True, "models": out}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "models": []}


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
