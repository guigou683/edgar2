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
"""


def init_db() -> None:
    """Crée le schéma s'il n'existe pas."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)


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
