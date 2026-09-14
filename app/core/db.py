"""Couche d'accès SQLite d'EDGAR v2.

- Schéma créé de façon idempotente au démarrage (init_db).
- Mode WAL pour supporter les accès simultanés (multi-postes).
- **Toutes** les requêtes sont paramétrées (anti-injection SQL).

Ce module ne contient que l'accès aux données ; la politique de sécurité
(hachage, sessions, anti-bruteforce) vit dans core/auth.py.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from core.config import settings


def now_iso() -> str:
    """Horodatage ISO 8601 en UTC (stockage homogène)."""
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Ouvre une connexion SQLite (une par opération), commit/rollback gérés."""
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE,
    email                TEXT,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'user',      -- user|contributor|admin
    status               TEXT NOT NULL DEFAULT 'pending',   -- pending|active|suspended
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,                            -- jeton opaque
    user_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    ip          TEXT,
    user_agent  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    user_id     INTEGER,
    username    TEXT,
    action      TEXT NOT NULL,
    ip          TEXT,
    user_agent  TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER PRIMARY KEY,
    value_json TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    base_id    TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, base_id);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT NOT NULL,              -- user|assistant
    content         TEXT NOT NULL,
    sources_json    TEXT,
    diagnostics_json TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    base_id    TEXT NOT NULL,
    filename   TEXT NOT NULL,
    status     TEXT NOT NULL,                   -- indexed | failed | pending
    chunks     INTEGER NOT NULL DEFAULT 0,
    pages      INTEGER NOT NULL DEFAULT 0,
    strategy   TEXT,
    size       INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(base_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_docs_base ON documents(base_id);

CREATE TABLE IF NOT EXISTS document_summaries (
    base_id    TEXT NOT NULL,
    filename   TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'short',   -- 'short' (aperçu) | 'long' (map-reduce)
    summary    TEXT NOT NULL,
    model      TEXT,
    chunks     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT,
    PRIMARY KEY (base_id, filename, kind)
);
"""


def init_db() -> None:
    """Crée le schéma s'il n'existe pas, puis applique les migrations légères."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """Migrations idempotentes sur bases existantes."""
    # document_summaries : ajout de la colonne `kind` (résumé court / long) + PK élargie.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(document_summaries)")]
    if cols and "kind" not in cols:
        conn.executescript(
            "ALTER TABLE document_summaries RENAME TO _ds_old;"
            "CREATE TABLE document_summaries ("
            "  base_id TEXT NOT NULL, filename TEXT NOT NULL,"
            "  kind TEXT NOT NULL DEFAULT 'short', summary TEXT NOT NULL, model TEXT,"
            "  chunks INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, created_by TEXT,"
            "  PRIMARY KEY (base_id, filename, kind));"
            "INSERT INTO document_summaries "
            "  (base_id, filename, kind, summary, model, chunks, created_at, created_by)"
            "  SELECT base_id, filename, 'short', summary, model, chunks, created_at, created_by"
            "  FROM _ds_old;"
            "DROP TABLE _ds_old;")


# --------------------------------------------------------------------------
# Utilisateurs
# --------------------------------------------------------------------------
def create_user(username: str, password_hash: str, role: str, status: str,
                email: Optional[str] = None, must_change_password: bool = False) -> int:
    ts = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, email, password_hash, role, status, "
            "must_change_password, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (username, email, password_hash, role, status,
             1 if must_change_password else 0, ts, ts),
        )
        return int(cur.lastrowid)


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def count_users() -> int:
    with get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def list_users() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, email, role, status, must_change_password, "
            "failed_attempts, locked_until, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def update_password(user_id: int, password_hash: str, must_change: bool = False) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ?, "
            "updated_at = ? WHERE id = ?",
            (password_hash, 1 if must_change else 0, now_iso(), user_id),
        )


def set_status(user_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                     (status, now_iso(), user_id))


def set_role(user_id: int, role: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                     (role, now_iso(), user_id))


def update_user_identity(user_id: int, username: str, email: Optional[str]) -> None:
    """Met à jour le nom d'utilisateur et le courriel (correction admin)."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET username = ?, email = ?, updated_at = ? WHERE id = ?",
                     (username, email, now_iso(), user_id))


def delete_user(user_id: int) -> None:
    """Supprime définitivement un compte. Les sessions, réglages et conversations
    (donc messages) suivent via ON DELETE CASCADE."""
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def record_login_failure(user_id: int, failed_attempts: int, locked_until: Optional[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET failed_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
            (failed_attempts, locked_until, now_iso(), user_id),
        )


def reset_login_failures(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), user_id),
        )


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def insert_session(sid: str, user_id: int, expires_at: str,
                   ip: Optional[str], user_agent: Optional[str]) -> None:
    ts = now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, last_seen, expires_at, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, user_id, ts, ts, expires_at, ip, user_agent),
        )


def get_session(sid: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None


def touch_session(sid: str, expires_at: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE id = ?",
                     (now_iso(), expires_at, sid))


def revoke_session(sid: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (sid,))


def revoke_user_sessions(user_id: int) -> None:
    """Révoque immédiatement toutes les sessions d'un compte (rétrogradation, suspension…)."""
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))


def count_active_sessions() -> int:
    """Nombre de sessions actives (non révoquées, non expirées)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) n FROM sessions "
            "WHERE (revoked IS NULL OR revoked = 0) AND expires_at > ?",
            (now_iso(),)).fetchone()["n"]


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
def insert_audit(action: str, user_id: Optional[int], username: Optional[str],
                 ip: Optional[str], user_agent: Optional[str], detail: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, user_id, username, action, ip, user_agent, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), user_id, username, action, ip, user_agent, detail),
        )


def list_audit(limit: int = 200) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Conversations & messages
# --------------------------------------------------------------------------
def create_conversation(user_id: int, base_id: str, title: str) -> int:
    ts = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (user_id, base_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, base_id, title[:120], ts, ts),
        )
        return int(cur.lastrowid)


def get_conversation(conv_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return dict(row) if row else None


def list_conversations(user_id: int, base_id: Optional[str] = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if base_id:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? AND base_id = ? "
                "ORDER BY updated_at DESC", (user_id, base_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,)).fetchall()
        return [dict(r) for r in rows]


def touch_conversation(conv_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                     (now_iso(), conv_id))


def rename_conversation(conv_id: int, title: str) -> None:
    """Renomme une conversation (updated_at inchangé : pas de remontée en tête de liste)."""
    with get_conn() as conn:
        conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))


def add_message(conversation_id: int, role: str, content: str,
                sources_json: Optional[str] = None,
                diagnostics_json: Optional[str] = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, sources_json, "
            "diagnostics_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, sources_json, diagnostics_json, now_iso()),
        )
        return int(cur.lastrowid)


def delete_conversation(conv_id: int) -> None:
    """Supprime une conversation (les messages suivent via ON DELETE CASCADE)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def delete_conversations_for_base(user_id: int, base_id: str) -> int:
    """Supprime toutes les conversations d'un utilisateur pour une base. Renvoie le nombre."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM conversations WHERE user_id = ? AND base_id = ?",
                           (user_id, base_id))
        return cur.rowcount


def list_messages(conversation_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Réglages globaux (clé/valeur JSON)
# --------------------------------------------------------------------------
def get_setting(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value_json"] if row else None


def set_setting(key: str, value_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value_json) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (key, value_json),
        )


# --------------------------------------------------------------------------
# Registre des documents importés
# --------------------------------------------------------------------------
def upsert_document(base_id: str, filename: str, status: str, chunks: int = 0,
                    pages: int = 0, strategy: Optional[str] = None,
                    size: int = 0, error: Optional[str] = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (base_id, filename, status, chunks, pages, strategy, "
            "size, error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(base_id, filename) DO UPDATE SET status=excluded.status, "
            "chunks=excluded.chunks, pages=excluded.pages, strategy=excluded.strategy, "
            "size=excluded.size, error=excluded.error, updated_at=excluded.updated_at",
            (base_id, filename, status, chunks, pages, strategy, size, error, now_iso()),
        )


def list_documents(base_id: str, query: Optional[str] = None,
                   status: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM documents WHERE base_id = ?"
    args: list[Any] = [base_id]
    if query:
        sql += " AND filename LIKE ?"
        args.append(f"%{query}%")
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY filename"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_document(base_id: str, filename: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM documents WHERE base_id = ? AND filename = ?",
                           (base_id, filename)).fetchone()
        return dict(row) if row else None


def list_document_names(base_id: str) -> list[str]:
    """Uniquement les noms de fichiers (léger) — pour construire l'arbre de dossiers."""
    with get_conn() as conn:
        return [r["filename"] for r in conn.execute(
            "SELECT filename FROM documents WHERE base_id = ?", (base_id,)).fetchall()]


def document_overview(base_id: str) -> dict[str, Any]:
    """Statistiques agrégées d'une base, calculées en SQL (sans charger les lignes)."""
    with get_conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(chunks),0) chunks, COALESCE(SUM(pages),0) pages, "
            "COALESCE(SUM(size),0) size FROM documents WHERE base_id = ?", (base_id,)).fetchone()
        by_status = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) n FROM documents WHERE base_id = ? GROUP BY status", (base_id,))}
        by_strategy = {(row["strategy"] or "—"): row["n"] for row in conn.execute(
            "SELECT strategy, COUNT(*) n FROM documents WHERE base_id = ? GROUP BY strategy", (base_id,))}
    return {"files": r["n"], "chunks": r["chunks"], "pages": r["pages"], "size": r["size"],
            "by_status": by_status, "by_strategy": by_strategy}


def list_documents_page(base_id: str, query: Optional[str] = None, status: Optional[str] = None,
                        folder: Optional[str] = None, offset: int = 0,
                        limit: int = 100) -> tuple[list[dict[str, Any]], int]:
    """Listing paginé + filtré (nom, statut, sous-dossier). Renvoie (lignes, total)."""
    where = "WHERE base_id = ?"
    args: list[Any] = [base_id]
    if query:
        where += " AND filename LIKE ?"
        args.append(f"%{query}%")
    if status:
        where += " AND status = ?"
        args.append(status)
    if folder:
        where += " AND filename LIKE ?"
        args.append(f"{folder.rstrip('/')}/%")
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) n FROM documents {where}", args).fetchone()["n"]
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM documents {where} ORDER BY filename LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()]
    return rows, total


def delete_document_row(base_id: str, filename: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM documents WHERE base_id = ? AND filename = ?",
                     (base_id, filename))
        # Le résumé mis en cache devient caduc si le document disparaît.
        conn.execute("DELETE FROM document_summaries WHERE base_id = ? AND filename = ?",
                     (base_id, filename))


# --------------------------------------------------------------------------
# Résumés de documents (cache LLM : généré à la demande, réutilisé ensuite)
# --------------------------------------------------------------------------
def get_document_summary(base_id: str, filename: str,
                         kind: str = "short") -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM document_summaries WHERE base_id = ? AND filename = ? AND kind = ?",
            (base_id, filename, kind)).fetchone()
        return dict(row) if row else None


def list_document_summaries(base_id: str) -> list[dict[str, Any]]:
    """Tous les résumés d'une base (tous types) — pour la synchronisation hors-ligne."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM document_summaries WHERE base_id = ?", (base_id,)).fetchall()
        return [dict(r) for r in rows]


def save_document_summary(base_id: str, filename: str, summary: str,
                          model: Optional[str], chunks: int, created_by: Optional[str],
                          kind: str = "short") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO document_summaries (base_id, filename, kind, summary, model, chunks, "
            "created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(base_id, filename, kind) DO UPDATE SET summary=excluded.summary, "
            "model=excluded.model, chunks=excluded.chunks, created_at=excluded.created_at, "
            "created_by=excluded.created_by",
            (base_id, filename, kind, summary, model, chunks, now_iso(), created_by))


def delete_document_summary(base_id: str, filename: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM document_summaries WHERE base_id = ? AND filename = ?",
                     (base_id, filename))


def count_documents(base_id: str) -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM documents WHERE base_id = ? GROUP BY status",
            (base_id,)).fetchall()
        return {r["status"]: r["n"] for r in rows}


def get_user_settings(user_id: int) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value_json FROM user_settings WHERE user_id = ?",
                           (user_id,)).fetchone()
        return row["value_json"] if row else None


def set_user_settings(user_id: int, value_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, value_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET value_json = excluded.value_json",
            (user_id, value_json),
        )


def delete_user_settings(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
