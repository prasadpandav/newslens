"""SQLite storage. One file, plain SQL, inspectable with any SQLite browser."""
import json
import sqlite3
import time
import uuid
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, token TEXT, context TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY, url TEXT UNIQUE, title TEXT, summary TEXT, source TEXT,
  topic TEXT, published REAL, entities TEXT, fetched_at REAL, group_id TEXT);
CREATE TABLE IF NOT EXISTS trends (
  id TEXT PRIMARY KEY, kind TEXT, name TEXT, narrative TEXT, sectors TEXT,
  regions TEXT, article_ids TEXT, velocity REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS connections (
  id TEXT PRIMARY KEY, article_a TEXT, article_b TEXT, chain TEXT,
  confidence REAL, affected TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS stories (
  id TEXT PRIMARY KEY, headline TEXT, narrative TEXT, credibility REAL,
  credibility_note TEXT, claims TEXT, topic TEXT, article_ids TEXT,
  trend_ids TEXT, connection_ids TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS feed_items (
  id TEXT PRIMARY KEY, user_id TEXT, story_id TEXT, impact_text TEXT,
  impact_score INTEGER, created_at REAL, UNIQUE(user_id, story_id));
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, user_id TEXT, story_id TEXT, action TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS bookmarks (
  id TEXT PRIMARY KEY, user_id TEXT, story_id TEXT, created_at REAL,
  UNIQUE(user_id, story_id));
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY, title TEXT, prediction TEXT, chain TEXT, watch TEXT,
  affected TEXT, horizon TEXT, confidence REAL, story_ids TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS live_cards (
  id TEXT PRIMARY KEY, type TEXT, priority REAL, title TEXT, subtitle TEXT,
  detail TEXT, story_id TEXT, url TEXT, payload TEXT,
  starts_at REAL, ends_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, stage TEXT, status TEXT, detail TEXT,
  llm_calls INTEGER DEFAULT 0, llm_tokens INTEGER DEFAULT 0, created_at REAL);
-- Analytics. Deliberately no IP and no user-agent: `device` is a random
-- first-party id minted by the client, `country`/`ref` are derived at request
-- time and the address they came from is never written down.
CREATE TABLE IF NOT EXISTS visits (
  id TEXT PRIMARY KEY, device TEXT, day TEXT, path TEXT, ref TEXT,
  country TEXT, user_id TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS visits_day ON visits(day);
CREATE INDEX IF NOT EXISTS visits_device ON visits(device);
-- Pre-aggregated request counts: one row per (day, route), incremented in place.
-- Keeps overall-traffic reporting O(days) instead of one row per request.
CREATE TABLE IF NOT EXISTS traffic (
  day TEXT, route TEXT, hits INTEGER DEFAULT 0, PRIMARY KEY (day, route));
"""

_schema_ready = False

def connect():
    global _schema_ready
    # timeout: wait up to 30s for a write lock instead of failing instantly. The
    # 3h pipeline, the ~5min live-refresh job, and web requests all write to this
    # one file from different threads, so contention is expected.
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL lets readers and one writer work concurrently (no more "database is
    # locked" on every overlap); busy_timeout makes writers queue for the lock.
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass  # pragmas are best-effort; the connection still works without them
    if not _schema_ready:  # once per process, not on every request
        con.executescript(SCHEMA)
        # Idempotent migrations for existing databases.
        for col in ("google_sub TEXT", "email TEXT", "name TEXT", "picture TEXT"):
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            con.execute("ALTER TABLE articles ADD COLUMN group_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Soft retirement: a trend the latest run no longer sees is stamped here
        # instead of being deleted, so existing links keep resolving. NULL = live.
        try:
            con.execute("ALTER TABLE trends ADD COLUMN retired_at REAL")
        except sqlite3.OperationalError:
            pass  # column already exists
        _schema_ready = True
    return con

def new_id():
    return uuid.uuid4().hex[:12]

def now():
    return time.time()

def j(obj):
    return json.dumps(obj, ensure_ascii=False)

def uj(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}

def log_run(con, stage, status, detail="", llm_calls=0, llm_tokens=0):
    # Best-effort: logging must never crash the caller (this insert is often the
    # LAST thing an error handler does, so a lock here would mask the real error).
    try:
        con.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            (new_id(), stage, status, detail, llm_calls, llm_tokens, now()))
        con.commit()
    except sqlite3.OperationalError:
        pass
