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
"""

_schema_ready = False

def connect():
    global _schema_ready
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
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
    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (new_id(), stage, status, detail, llm_calls, llm_tokens, now()))
    con.commit()
