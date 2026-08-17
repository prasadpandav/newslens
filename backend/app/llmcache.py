"""Answer cache for identical prompts.

The pipeline is re-runnable on purpose and each stage skips work already done —
but "done" is tracked at the level of a stored row, not of a prompt. A stage that
dies halfway (rate limit, OOM, deploy) leaves the items it DID process stored and
the rest untouched, and the next run re-asks only the missing ones. That part is
already efficient. What isn't: a stage re-run for any other reason (an admin
re-trigger, a stage retried after a downstream error, the same borderline pair
re-tested by a later run) re-sends prompts whose answers are still valid and still
sitting in the previous response.

So the key is the prompt itself, hashed, rather than any row id. Two callers that
construct byte-identical prompts get one billed call between them, regardless of
which stage they came from or whether either of them knows the other exists.

Deliberately NOT a general-purpose cache:
  * only whole `complete_json` answers, stored as the JSON they parsed to;
  * TTL'd (config.LLM_CACHE_TTL_SECONDS), because a prompt over a news window
    means something different tomorrow than it did today;
  * best-effort throughout, exactly like llmcost — a cache that raises is worse
    than no cache, since it would fail calls that would otherwise have succeeded.

Correctness note: a miss costs a call, a false HIT costs correctness. So the key
includes the task name as well as the prompt, and nothing normalises or truncates
the prompt before hashing — two prompts that differ by one character are two
different questions.
"""
import hashlib
import json
import sqlite3
import time
from . import config, db


def _key(task, prompt):
    h = hashlib.sha256()
    h.update(str(task or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt.encode("utf-8", "replace"))
    return h.hexdigest()


def get(task, prompt, con=None):
    """The cached answer for this exact (task, prompt), or None.

    None means "call the provider" — for a miss, an expired row, a malformed
    row, or any storage error at all.
    """
    ttl = config.LLM_CACHE_TTL_SECONDS
    if ttl <= 0:
        return None
    own = con is None
    try:
        if own:
            con = db.connect()
        row = con.execute(
            "SELECT response, created_at FROM llm_cache WHERE hash = ?",
            (_key(task, prompt),)).fetchone()
        if not row:
            return None
        if (row["created_at"] or 0) < time.time() - ttl:
            return None          # expired; swept by purge(), not on the read path
        return json.loads(row["response"])
    except (sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        if own and con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def put(task, prompt, value, con=None):
    """Store one answer. Returns True when written. Never raises."""
    if config.LLM_CACHE_TTL_SECONDS <= 0 or value is None:
        return False
    own = con is None
    try:
        if own:
            con = db.connect()
        con.execute(
            "INSERT INTO llm_cache (hash, task, response, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(hash) DO UPDATE SET response = excluded.response, "
            "  created_at = excluded.created_at",
            (_key(task, prompt), str(task or "")[:40], json.dumps(value), time.time()))
        con.commit()
        return True
    except (sqlite3.Error, ValueError, TypeError):
        return False
    finally:
        if own and con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def drop(task, prompt, con=None):
    """Delete one cached answer. Returns True when a row went.

    For the case where the STORED answer is the problem: a wrong-shaped row
    written before shape validation existed will otherwise keep being served
    for the whole TTL, replaying the same caller crash without ever calling a
    provider that could return something better."""
    own = con is None
    try:
        if own:
            con = db.connect()
        n = con.execute("DELETE FROM llm_cache WHERE hash = ?",
                        (_key(task, prompt),)).rowcount
        con.commit()
        return bool(n)
    except (sqlite3.Error, ValueError, TypeError):
        return False
    finally:
        if own and con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def purge(con, ttl=None):
    """Delete expired rows. Called once per pipeline run — the read path only
    IGNORES an expired row, so without this the table would keep every prompt
    ever sent. Returns how many were dropped."""
    ttl = config.LLM_CACHE_TTL_SECONDS if ttl is None else ttl
    if ttl <= 0:
        return 0
    try:
        n = con.execute("DELETE FROM llm_cache WHERE created_at < ?",
                        (time.time() - ttl,)).rowcount
        con.commit()
        return n
    except sqlite3.Error:
        return 0


def stats(con):
    """Row count and age span, for /admin/usage."""
    try:
        r = con.execute("SELECT COUNT(*) n, MIN(created_at) oldest, "
                        "MAX(created_at) newest FROM llm_cache").fetchone()
        return {"rows": r["n"] or 0, "oldest": r["oldest"], "newest": r["newest"],
                "ttl_seconds": config.LLM_CACHE_TTL_SECONDS}
    except sqlite3.Error:
        return {}
