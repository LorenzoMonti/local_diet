"""SQLite storage. Two users, so plain sqlite3 is plenty."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "dieta.db"
UPLOAD_DIR = DATA_DIR / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    exam_date TEXT,
    anon_text TEXT,
    approved INTEGER NOT NULL DEFAULT 0,
    labs TEXT,
    anon_method TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),  -- NULL = nota condivisa
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manuale',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    status TEXT NOT NULL DEFAULT 'generating',  -- generating | ready | error
    request TEXT,
    error TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS shopping_items (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER REFERENCES plans(id),
    name TEXT NOT NULL,
    quantity REAL,
    unit TEXT,
    category TEXT NOT NULL DEFAULT 'Altro',
    checked INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL DEFAULT 'Nuova chat',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    context_hash TEXT,
    provider TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,          -- user | assistant | system
    content TEXT NOT NULL,       -- JSON, esattamente come inviato all'API
    display TEXT,                -- testo da mostrare in UI (NULL = nascosto)
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with tx() as c:
        c.executescript(SCHEMA)
        # Migrazioni leggere per database creati con versioni precedenti.
        for table, col in (("files", "anon_method"), ("conversations", "provider"), ("plans", "source")):
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            names = [n.strip() for n in os.environ.get("USER_NAMES", "Persona 1,Persona 2").split(",")]
            for name in names[:2]:
                c.execute("INSERT INTO users (name) VALUES (?)", (name,))


# --- helpers ---------------------------------------------------------------

def users() -> list[dict]:
    with tx() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [{**dict(r), "profile": json.loads(r["profile"])} for r in rows]


def user(uid: int) -> dict | None:
    return next((u for u in users() if u["id"] == uid), None)


def active_plan() -> dict | None:
    """Il piano scelto come attivo; in mancanza (o se eliminato) il più recente pronto."""
    with tx() as c:
        chosen = c.execute("SELECT value FROM settings WHERE key='active_plan_id'").fetchone()
        r = None
        if chosen and chosen["value"]:
            r = c.execute("SELECT * FROM plans WHERE id=? AND status='ready'", (chosen["value"],)).fetchone()
        if r is None:
            r = c.execute("SELECT * FROM plans WHERE status='ready' ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    return {**dict(r), "data": json.loads(r["data"])}


def notes_for(uid: int | None = None) -> list[dict]:
    with tx() as c:
        rows = c.execute(
            "SELECT n.*, u.name AS user_name FROM notes n LEFT JOIN users u ON u.id = n.user_id ORDER BY n.id"
        ).fetchall()
    return [dict(r) for r in rows]


def approved_labs() -> list[dict]:
    with tx() as c:
        rows = c.execute(
            "SELECT f.*, u.name AS user_name FROM files f JOIN users u ON u.id = f.user_id "
            "WHERE f.approved = 1 ORDER BY f.user_id, COALESCE(f.exam_date, f.uploaded_at)"
        ).fetchall()
    return [{**dict(r), "labs": json.loads(r["labs"]) if r["labs"] else None} for r in rows]


def get_setting(key: str, default: str | None = None) -> str | None:
    with tx() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] else default


def set_setting(key: str, value: str | None):
    with tx() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value or None))


def activate_plan(plan_id: int):
    """Rende attivo un piano: la lista della spesa lo segue, le voci extra vengono portate con sé."""
    set_setting("active_plan_id", str(plan_id))
    with tx() as c:
        c.execute("UPDATE shopping_items SET plan_id=? WHERE manual=1", (plan_id,))
