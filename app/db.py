"""SQLite (WAL) com SQL puro. Um arquivo no volume /data; a conexao e aberta por chamada (barato e seguro entre threads)."""
import os
import sqlite3
from contextlib import contextmanager

DATA = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA, "aluno.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'aluno', pw_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  must_change INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS courses (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, subtitle TEXT DEFAULT '',
  description TEXT DEFAULT '', published INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS modules (
  id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL, title TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY, module_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0, video_status TEXT NOT NULL DEFAULT 'none', duration REAL DEFAULT 0,
  error TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY, course_id INTEGER, title TEXT NOT NULL, description TEXT DEFAULT '',
  filename TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entitlements (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, resource_type TEXT NOT NULL, resource_id INTEGER NOT NULL,
  source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', starts_at TEXT, ends_at TEXT,
  granted_by INTEGER, reason TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ent ON entitlements(user_id, resource_type, resource_id);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor_id INTEGER, action TEXT NOT NULL, target TEXT DEFAULT '', detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS progress (
  user_id INTEGER NOT NULL, lesson_id INTEGER NOT NULL, seconds REAL NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, PRIMARY KEY (user_id, lesson_id));
CREATE TABLE IF NOT EXISTS mentorias (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '', mentor_id INTEGER NOT NULL,
  starts_at TEXT NOT NULL, duration_min INTEGER NOT NULL DEFAULT 60, open_before_min INTEGER NOT NULL DEFAULT 10,
  close_after_min INTEGER NOT NULL DEFAULT 30, max_participants INTEGER NOT NULL DEFAULT 5,
  status TEXT NOT NULL DEFAULT 'agendada', record_policy TEXT NOT NULL DEFAULT 'nao', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mentoria_participants (
  mentoria_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (mentoria_id, user_id));
CREATE TABLE IF NOT EXISTS recordings (
  id INTEGER PRIMARY KEY, mentoria_id INTEGER NOT NULL, title TEXT NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recording_grants (
  recording_id INTEGER NOT NULL, user_id INTEGER NOT NULL, source TEXT NOT NULL, granted_at TEXT NOT NULL, revoked_at TEXT,
  PRIMARY KEY (recording_id, user_id));
CREATE TABLE IF NOT EXISTS consents (
  id INTEGER PRIMARY KEY, mentoria_id INTEGER NOT NULL, user_id INTEGER NOT NULL, version TEXT NOT NULL, accepted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id TEXT PRIMARY KEY, kind TEXT, received_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS kiwify_products (
  kiwify_id TEXT PRIMARY KEY, course_id INTEGER, asset_id INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS badges_earned (
  user_id INTEGER NOT NULL, badge TEXT NOT NULL, earned_at TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, badge));
CREATE TABLE IF NOT EXISTS attendance (
  mentoria_id INTEGER NOT NULL, user_id INTEGER NOT NULL, joined_at TEXT NOT NULL, PRIMARY KEY (mentoria_id, user_id));
CREATE TABLE IF NOT EXISTS purchases (
  order_id TEXT PRIMARY KEY, ref TEXT NOT NULL DEFAULT '', user_id INTEGER NOT NULL, product_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_tokens (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS certificates (
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, user_id INTEGER NOT NULL, course_id INTEGER NOT NULL,
  student_name TEXT NOT NULL, course_title TEXT NOT NULL, minutes INTEGER NOT NULL DEFAULT 0, pct INTEGER NOT NULL DEFAULT 0,
  issued_at TEXT NOT NULL, UNIQUE (user_id, course_id));
"""

# colunas novas em tabelas que ja existem em producao (SQLite nao tem ADD COLUMN IF NOT EXISTS)
MIGRATIONS = [
    ("users", "photo", "ALTER TABLE users ADD COLUMN photo TEXT NOT NULL DEFAULT ''"),
    ("kiwify_products", "pay_url", "ALTER TABLE kiwify_products ADD COLUMN pay_url TEXT NOT NULL DEFAULT ''"),
    ("kiwify_products", "downsell_id", "ALTER TABLE kiwify_products ADD COLUMN downsell_id TEXT NOT NULL DEFAULT ''"),
    ("kiwify_products", "label", "ALTER TABLE kiwify_products ADD COLUMN label TEXT NOT NULL DEFAULT ''"),
    ("kiwify_products", "plan", "ALTER TABLE kiwify_products ADD COLUMN plan INTEGER NOT NULL DEFAULT 0"),
    ("kiwify_products", "all_courses", "ALTER TABLE kiwify_products ADD COLUMN all_courses INTEGER NOT NULL DEFAULT 0"),
    ("kiwify_products", "support", "ALTER TABLE kiwify_products ADD COLUMN support INTEGER NOT NULL DEFAULT 0"),
]


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    os.makedirs(DATA, exist_ok=True)
    for sub in ("src", "media", "assets", "recordings", "tmp", "photos"):
        os.makedirs(os.path.join(DATA, sub), exist_ok=True)
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]:
                c.execute(ddl)


def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def x(sql, args=()):
    """Executa e devolve o id da linha inserida."""
    with conn() as c:
        return c.execute(sql, args).lastrowid
