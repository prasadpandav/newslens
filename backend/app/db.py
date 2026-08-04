"""SQLite storage. One file, plain SQL, inspectable with any SQLite browser."""
import json
import sqlite3
import time
import uuid
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, token TEXT, context TEXT, created_at REAL);
-- image_url is a URL only, never bytes: clients load it straight from the
-- publisher's CDN. Storing artwork here would put multi-MB blobs on a 512MB
-- instance with a small disk. width/height are what the feed declared (or what
-- was parsed out of the URL) and exist so images.best_of can rank a story's
-- articles later without re-reading the feed.
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY, url TEXT UNIQUE, title TEXT, summary TEXT, source TEXT,
  topic TEXT, published REAL, entities TEXT, fetched_at REAL, group_id TEXT,
  image_url TEXT, image_width INTEGER, image_height INTEGER);
CREATE TABLE IF NOT EXISTS trends (
  id TEXT PRIMARY KEY, kind TEXT, name TEXT, narrative TEXT, sectors TEXT,
  regions TEXT, article_ids TEXT, velocity REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS connections (
  id TEXT PRIMARY KEY, article_a TEXT, article_b TEXT, chain TEXT,
  confidence REAL, affected TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS stories (
  id TEXT PRIMARY KEY, headline TEXT, narrative TEXT, credibility REAL,
  credibility_note TEXT, claims TEXT, topic TEXT, article_ids TEXT,
  trend_ids TEXT, connection_ids TEXT, created_at REAL, image_url TEXT);
CREATE TABLE IF NOT EXISTS feed_items (
  id TEXT PRIMARY KEY, user_id TEXT, story_id TEXT, impact_text TEXT,
  impact_score INTEGER, created_at REAL, UNIQUE(user_id, story_id));
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, user_id TEXT, story_id TEXT, action TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS bookmarks (
  id TEXT PRIMARY KEY, user_id TEXT, story_id TEXT, created_at REAL,
  UNIQUE(user_id, story_id));
-- A story a user has seen (opened) or explicitly dismissed. Kept out of /feed
-- once present so the feed doesn't go stale with things already read; listed
-- back on request so it isn't just gone.
CREATE TABLE IF NOT EXISTS read_stories (
  id TEXT PRIMARY KEY, user_id TEXT, story_id TEXT, created_at REAL,
  UNIQUE(user_id, story_id));
-- How a story's corroboration has moved over time. One row per story per
-- pipeline run, written ONLY when something actually changed (see
-- Storyteller._record_history), so a quiet story costs nothing.
--
-- This is what makes "68 -> 82 · two new primaries" on the feed rail, "moved
-- most this week" on Trends, and "▲14 since you saved" on Saved into real
-- claims rather than guesses. The client previously diffed against a
-- localStorage snapshot, which could only ever say "since YOU last looked" on
-- one device; this is global and survives a cache clear.
--
-- Keyed by story_id AND event_id: a storyline that gets retold keeps its
-- event_id across runs even when the story row is replaced, so history follows
-- the event rather than the row. Pruned by prune_history() — unbounded growth
-- is exactly what the 512MB box cannot afford.
CREATE TABLE IF NOT EXISTS story_history (
  id TEXT PRIMARY KEY, story_id TEXT, event_id TEXT, credibility REAL,
  source_count INTEGER, verified INTEGER, disputed INTEGER, unverified INTEGER,
  conflicts INTEGER, created_at REAL);
CREATE INDEX IF NOT EXISTS story_history_story ON story_history(story_id, created_at);
CREATE INDEX IF NOT EXISTS story_history_created ON story_history(created_at);
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
-- /feed and /signals filter or order by these; without an index each call
-- full-scans the table. (trends' index is created after its ALTER TABLE below,
-- since it covers a column that doesn't exist in this CREATE TABLE.)
CREATE INDEX IF NOT EXISTS stories_created ON stories(created_at);
CREATE INDEX IF NOT EXISTS signals_created ON signals(created_at);
-- `articles` had NO index beyond its id/url keys, so both of Scout's hot reads
-- full-scanned the whole table: assign_groups' window read (once per run) and
-- Scout._is_same_source_dup (once per INGESTED ENTRY — ~557 scans per run).
-- Confirmed SCAN -> SEARCH on both with EXPLAIN QUERY PLAN.
CREATE INDEX IF NOT EXISTS articles_fetched ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS articles_source_fetched ON articles(source, fetched_at);
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
        # Story artwork. Existing rows stay NULL until the next run re-reads
        # their feed (POST /admin/backfill-images does that on demand); both
        # clients treat a missing image as "render the text-only card", so an
        # un-backfilled row degrades instead of breaking.
        for col in ("image_url TEXT", "image_width INTEGER", "image_height INTEGER"):
            try:
                con.execute(f"ALTER TABLE articles ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            con.execute("ALTER TABLE stories ADD COLUMN image_url TEXT")
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
        # Story bodies are two distinct pieces: `narrative` holds the full
        # "what happened" storyline (unchanged meaning, so every existing reader
        # — feed previews, OG descriptions, search — keeps working), and this
        # holds "why it matters". Older rows have NULL here and fall back to the
        # legacy first-paragraph/rest split on the client.
        try:
            con.execute("ALTER TABLE stories ADD COLUMN why_matters TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # When a developing item was last retold, kept separate from created_at
        # (first told), which is now immutable. These used to be one column: a
        # retell stamped created_at = now, which reset the item's age to zero in
        # ranking.rank_score and let a storyline that accreted a single article
        # per run sit at rank #1 indefinitely — the exact staleness the ranking
        # module exists to prevent.
        #
        # Backfilled to created_at rather than left NULL, and never written NULL
        # afterwards, because readers previously had to say
        # COALESCE(updated_at, created_at) — an expression over two columns, which
        # no index can serve. That turned every feed/trends/signals read into a
        # full table SCAN plus a temp B-TREE sort (confirmed via EXPLAIN QUERY
        # PLAN) instead of an index SEARCH, and the per-event story split
        # multiplies the row count those scans walk. With the column always
        # populated, readers use the bare column and the indexes below apply.
        for table in ("stories", "trends", "signals"):
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN updated_at REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Idempotent: only ever touches rows still carrying the old NULL.
            con.execute(f"UPDATE {table} SET updated_at = created_at "
                        f"WHERE updated_at IS NULL")
        con.execute("CREATE INDEX IF NOT EXISTS stories_updated "
                    "ON stories(updated_at)")
        con.execute("CREATE INDEX IF NOT EXISTS signals_updated "
                    "ON signals(updated_at)")
        # Which EVENT a story tells — the articles' shared group_id. A story used
        # to be identified by its macro trend, but a trend is a theme spanning
        # many separate events, so every event of a theme was folded into one row.
        # NULL marks a legacy row written under that scheme.
        try:
            con.execute("ALTER TABLE stories ADD COLUMN event_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # /trends filters WHERE kind=? AND retired_at IS NULL on every request.
        # Retired rows are now kept (not deleted) for TREND_RETIRE_PURGE_DAYS, so
        # without this index that scan gets slower every day instead of staying
        # flat — must run after the ALTER TABLE above, since it covers that column.
        # /trends always filters kind + retired_at and then orders by recency, so
        # carrying updated_at in the same index lets one seek serve all three.
        con.execute("CREATE INDEX IF NOT EXISTS trends_kind_retired "
                   "ON trends(kind, retired_at, updated_at)")
        # ---- redesign phases 1-3 ----
        # `merge_stats`: the {facts, core, unique, conflicts, sources} dict that
        # textmerge.build_brief has always returned and the Storyteller has
        # always thrown away. `conflicts` is literally "points where outlets
        # report different numbers", which is the design's "framing spread" /
        # "where they differ" — free, since the merge already computes it.
        try:
            con.execute("ALTER TABLE stories ADD COLUMN merge_stats TEXT")
        except sqlite3.OperationalError:
            pass
        # How far into a story the reader actually got. `read_stories` only ever
        # recorded that a story was OPENED, which is why the Read screen cannot
        # honestly say "31 of 58 read in full" or name what you keep abandoning.
        for col in ("progress REAL", "completed_at REAL"):
            try:
                con.execute(f"ALTER TABLE read_stories ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        # Corroboration at the moment of saving, so Saved can say "▲14 since you
        # saved" without walking story_history for every row on every request.
        try:
            con.execute("ALTER TABLE bookmarks ADD COLUMN credibility_at_save REAL")
        except sqlite3.OperationalError:
            pass
        # ---- phase 4: the reader ----
        # `beats`  [{label, text}] — the storyline cut into navigable sections.
        # `anchors` [{claim, quote}] — the exact sentence written for each
        #           verified claim, so the margin can rule to the line it
        #           belongs to instead of floating beside the whole article.
        # Both are NULL on every story written before this shipped, and both
        # clients fall back to the single-narrative rendering when they are —
        # a mixed feed of old and new stories has to read correctly, so absence
        # is a supported state rather than something to backfill away.
        for col in ("beats TEXT", "anchors TEXT"):
            try:
                con.execute(f"ALTER TABLE stories ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        # Which beat the reader stopped in, for Saved's "you stopped at ...".
        try:
            con.execute("ALTER TABLE read_stories ADD COLUMN beat TEXT")
        except sqlite3.OperationalError:
            pass
        # ---- phase 5: how outlets framed it ----
        # {axis, positions, spread, missing} — where each outlet that carried
        # this event sat on a story-specific framing axis. NULL means "nobody
        # has asked yet", which is the normal state: this is computed on demand
        # when a reader opens the panel (like personalization), never in the
        # pipeline, so LLM spend tracks real reads instead of catalogue size.
        # Cleared on retell when the source set changes — the spread a new
        # outlet joins is not the spread we classified.
        try:
            con.execute("ALTER TABLE stories ADD COLUMN framing TEXT")
        except sqlite3.OperationalError:
            pass
        # COMMIT THE MIGRATION before flipping the flag. Without this the DDL
        # above sits in this connection's open transaction: it is visible here,
        # so a PRAGMA check passes, but no OTHER connection can see the new
        # columns — and because _schema_ready is already True, none of them will
        # ever re-run the ALTERs either. In production the request threads and
        # the scheduler's pipeline thread each hold their own connection, so the
        # first deploy after a schema change could serve "no such column" 500s
        # from every connection except the one that happened to migrate, until
        # something unrelated committed on it. Caught by the migration test
        # against a pre-phase database.
        con.commit()
        _schema_ready = True
    return con


# History is append-only and written every run, so it is the one table here
# that grows without a natural ceiling. Called once per pipeline run.
def prune_history(con, keep_days=None):
    keep = keep_days if keep_days is not None else config.HISTORY_KEEP_DAYS
    n = con.execute("DELETE FROM story_history WHERE created_at < ?",
                    (now() - keep * 86400,)).rowcount
    con.commit()
    return n

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
