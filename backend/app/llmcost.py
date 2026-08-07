"""Persistent LLM spend accounting: which provider, which model, how many
tokens, and what it cost.

`llm.usage` — the counters the admin page has always shown — lives in the
process. On a 512MB box that redeploys and gets OOM-killed, that means the
numbers reset to zero regularly and silently, so they can answer "what is this
run burning right now" and never "what did last month cost". This module is the
durable half: every real call is written through to the `llm_usage` table as it
happens, so a restart loses nothing.

Written through per call rather than buffered and flushed, deliberately. A
buffer is the faster design and the wrong one here: the failure mode this box
actually has is SIGKILL with no traceback (see the ConnectionFinder OOM), which
takes an in-memory buffer with it — losing exactly the accounting for the run
that went wrong. Calls are seconds apart and rate-limited to
LLM_MAX_CALLS_PER_MIN, so one small UPSERT per call is not a cost worth
optimising against.

Everything here is best-effort. Accounting must never be the reason a pipeline
stage fails: a lost row is cheaper than a lost story.

Two numbers are stored and they are not equally trustworthy. Token counts come
straight from the provider's own response and are the record of truth. Cost is
DERIVED from a rate card that lives in configuration and goes stale whenever a
provider edits its price page, so it can be recomputed in place from the current
card (`reprice`) without touching the tokens.
"""
import sqlite3
from . import analytics, config, db

#: Model names carry no provider prefix in a response, so both spellings are
#: accepted in the rate card: "gemini/gemini-2.5-flash" and "gemini-2.5-flash".
def _price_keys(provider, model):
    return [f"{provider}/{model}".lower(), str(model).lower()]


def price_for(provider, model):
    """([input, output, cached_input] $ per 1M tokens, source) for this model,
    or (None, "unpriced"). Operator-supplied rates win over the seeded ones —
    a released default is a starting point, the env var is the current truth."""
    for key in _price_keys(provider, model):
        if key in config.LLM_PRICES_ENV:
            return config.LLM_PRICES_ENV[key], "configured"
    for key in _price_keys(provider, model):
        if key in config.LLM_PRICE_DEFAULTS:
            return config.LLM_PRICE_DEFAULTS[key], "default"
    return None, "unpriced"


def cost_of(provider, model, prompt_tokens, completion_tokens, cached_tokens=0,
            peak=False):
    """Dollars for one call, or None when the model has no rate.

    None rather than 0.0 all the way through, so an unpriced model reads as
    "we do not know" instead of "it was free" — the report and the admin page
    both surface the difference. `cached_tokens` is the slice of the prompt the
    provider served from its own cache and bills at a lower rate (DeepSeek
    reports this); it is a SUBSET of prompt_tokens, so it is priced separately
    and subtracted from the full-rate portion rather than added on top."""
    rate, _ = price_for(provider, model)
    if rate is None:
        return None
    cached = max(0, min(int(cached_tokens or 0), int(prompt_tokens or 0)))
    fresh = max(0, int(prompt_tokens or 0) - cached)
    usd = (fresh * rate[0] + int(completion_tokens or 0) * rate[1]
           + cached * rate[2]) / 1_000_000
    # DeepSeek doubles EVERY billing item inside its declared peak windows.
    return usd * (2.0 if peak else 1.0)


def record(provider, model, task, prompt_tokens=0, completion_tokens=0,
           cached_tokens=0, total_tokens=0, latency_ms=0, failed=False,
           peak=False, con=None):
    """Add one call to the running per-(day, provider, model, task, peak) row.

    A failed call still lands here with calls=0 and failures=1: a provider that
    burns a minute of pipeline time returning 429s is a real operational cost
    even though it bills nothing, and the failure counts are what make a
    provider's true usefulness visible next to its price.

    Returns True when the row was written. Never raises."""
    own = con is None
    try:
        if own:
            con = db.connect()
        total = int(total_tokens or 0) or (int(prompt_tokens or 0)
                                           + int(completion_tokens or 0))
        usd = 0.0 if failed else (cost_of(provider, model, prompt_tokens,
                                          completion_tokens, cached_tokens,
                                          peak) or 0.0)
        now = db.now()
        con.execute(
            "INSERT INTO llm_usage (day, provider, model, task, peak, calls, "
            " failures, prompt_tokens, completion_tokens, cached_tokens, "
            " total_tokens, cost_usd, latency_ms, first_at, last_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day, provider, model, task, peak) DO UPDATE SET "
            "  calls = calls + excluded.calls,"
            "  failures = failures + excluded.failures,"
            "  prompt_tokens = prompt_tokens + excluded.prompt_tokens,"
            "  completion_tokens = completion_tokens + excluded.completion_tokens,"
            "  cached_tokens = cached_tokens + excluded.cached_tokens,"
            "  total_tokens = total_tokens + excluded.total_tokens,"
            "  cost_usd = cost_usd + excluded.cost_usd,"
            "  latency_ms = latency_ms + excluded.latency_ms,"
            "  last_at = excluded.last_at",
            (analytics.day_key(), str(provider or "?")[:32], str(model or "?")[:80],
             str(task or "")[:40], 1 if peak else 0,
             0 if failed else 1, 1 if failed else 0,
             int(prompt_tokens or 0), int(completion_tokens or 0),
             int(cached_tokens or 0), 0 if failed else total,
             usd, max(0, int(latency_ms or 0)), now, now))
        con.commit()
        return True
    except sqlite3.Error:
        # Best-effort by design: see the module docstring. A dropped row costs
        # a rounding error in a monthly total; raising here would cost a story.
        return False
    finally:
        if own and con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def reprice(con, days=0):
    """Recompute stored cost from the CURRENT rate card and return how many rows
    changed. `days` = 0 rewrites the whole table.

    Exists because the rate card is the one part of this that is routinely
    wrong at first: prices are usually configured after the first spend has
    already been recorded, and a provider's list price changes on its own
    schedule. Tokens are the durable record, so cost can always be rebuilt from
    them — which is also why an unknown price is stored as an explicit gap
    rather than as zero. Rows for a model that is still unpriced are left
    untouched.

    Note this rewrites history at TODAY's rates. That is the right answer for a
    rate that was missing or wrong, and the wrong one after a genuine mid-window
    price change — in that case narrow the window with `days`."""
    where, args = "", []
    if days and days > 0:
        where = " WHERE day >= ?"
        args = [analytics.day_key(db.now() - days * 86400)]
    changed = 0
    for r in con.execute("SELECT day, provider, model, task, peak, prompt_tokens,"
                         " completion_tokens, cached_tokens, cost_usd "
                         "FROM llm_usage" + where, args).fetchall():
        usd = cost_of(r["provider"], r["model"], r["prompt_tokens"],
                      r["completion_tokens"], r["cached_tokens"], bool(r["peak"]))
        if usd is None or abs(usd - (r["cost_usd"] or 0)) < 1e-12:
            continue
        con.execute("UPDATE llm_usage SET cost_usd = ? WHERE day = ? AND "
                    "provider = ? AND model = ? AND task = ? AND peak = ?",
                    (usd, r["day"], r["provider"], r["model"], r["task"], r["peak"]))
        changed += 1
    con.commit()
    return changed


def purge(con, retain_days=None):
    """Drop spend rows past LLM_USAGE_RETAIN_DAYS. Off by default (0): these are
    aggregates with no per-call detail in them, they do not grow with traffic,
    and cost history older than the retention window is exactly what a finance
    question asks for."""
    days = config.LLM_USAGE_RETAIN_DAYS if retain_days is None else retain_days
    if not days or days <= 0:
        return 0
    try:
        n = con.execute("DELETE FROM llm_usage WHERE day < ?",
                        (analytics.day_key(db.now() - days * 86400),)).rowcount
        con.commit()
        return n
    except sqlite3.Error:
        return 0


# ------------------------------------------------------------------ reporting

def _avg(rows):
    """Per-call averages, computed from the aggregate rather than stored.

    Storing an average would be storing a number that stops being true the next
    time the row is incremented; these are the "average usage per call per
    model" the report is asked for, derived at read time from counters that are
    always current."""
    for r in rows:
        calls = r.get("calls") or 0
        r["avg_tokens_per_call"] = round((r.get("total_tokens") or 0) / calls, 1) if calls else 0
        r["avg_cost_per_call"] = round((r.get("cost_usd") or 0) / calls, 6) if calls else 0
        r["avg_latency_ms"] = round((r.get("latency_ms") or 0) / calls) if calls else 0
        r.pop("latency_ms", None)
        attempts = calls + (r.get("failures") or 0)
        r["failure_rate"] = round((r.get("failures") or 0) / attempts * 100, 1) if attempts else 0
    return rows


_SUMS = ("SUM(calls) calls, SUM(failures) failures, SUM(prompt_tokens) prompt_tokens,"
         " SUM(completion_tokens) completion_tokens, SUM(cached_tokens) cached_tokens,"
         " SUM(total_tokens) total_tokens, SUM(cost_usd) cost_usd,"
         " SUM(latency_ms) latency_ms")


def report(con, days=30):
    """Everything the cost owner needs for one window: totals, then the same
    spend cut by model, by provider and by pipeline task, plus a daily series
    and the rate card the money numbers were computed from."""
    since_day = analytics.day_key(db.now() - days * 86400)
    rows = analytics._rows

    by_model = _avg(rows(con, f"SELECT provider, model, {_SUMS} FROM llm_usage "
                              "WHERE day >= ? GROUP BY provider, model "
                              "ORDER BY cost_usd DESC, total_tokens DESC", (since_day,)))
    by_provider = _avg(rows(con, f"SELECT provider, {_SUMS} FROM llm_usage "
                                 "WHERE day >= ? GROUP BY provider "
                                 "ORDER BY cost_usd DESC, total_tokens DESC", (since_day,)))
    by_task = _avg(rows(con, f"SELECT task, {_SUMS} FROM llm_usage "
                             "WHERE day >= ? AND task != '' GROUP BY task "
                             "ORDER BY cost_usd DESC, total_tokens DESC", (since_day,)))
    daily = analytics._dense_days(
        rows(con, "SELECT day, SUM(calls) calls, SUM(total_tokens) total_tokens,"
                  " SUM(cost_usd) cost_usd FROM llm_usage WHERE day >= ? "
                  "GROUP BY day ORDER BY day", (since_day,)),
        days, ("calls", "total_tokens", "cost_usd"))

    # The rate every model in this window was costed at, and which models had
    # none. Printed next to the money so the figure can be checked rather than
    # taken on trust — an unpriced model contributes real tokens and $0, and
    # that has to be visible or the total silently understates the bill.
    rates, unpriced = [], []
    for m in by_model:
        rate, source = price_for(m["provider"], m["model"])
        m["priced"] = source != "unpriced"
        entry = {"provider": m["provider"], "model": m["model"], "source": source,
                 "input_per_1m": rate[0] if rate else None,
                 "output_per_1m": rate[1] if rate else None,
                 "cached_input_per_1m": rate[2] if rate else None}
        rates.append(entry)
        if rate is None:
            unpriced.append({"provider": m["provider"], "model": m["model"],
                             "calls": m["calls"], "total_tokens": m["total_tokens"]})

    calls = sum(r["calls"] or 0 for r in by_provider)
    tokens = sum(r["total_tokens"] or 0 for r in by_provider)
    cost = sum(r["cost_usd"] or 0 for r in by_provider)
    failures = sum(r["failures"] or 0 for r in by_provider)
    peak_cost = con.execute("SELECT SUM(cost_usd) c FROM llm_usage "
                            "WHERE day >= ? AND peak = 1", (since_day,)).fetchone()["c"]
    return {"window_days": days,
            "calls": calls, "failures": failures, "total_tokens": tokens,
            "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in by_provider),
            "completion_tokens": sum(r["completion_tokens"] or 0 for r in by_provider),
            "cached_tokens": sum(r["cached_tokens"] or 0 for r in by_provider),
            "cost_usd": round(cost, 6),
            "avg_cost_per_call": round(cost / calls, 6) if calls else 0,
            "avg_tokens_per_call": round(tokens / calls, 1) if calls else 0,
            # Straight-line projection from the window's own daily rate. Labelled
            # as a projection everywhere it is shown: it assumes the next 30 days
            # look like the last, which a pipeline whose cadence or model mix has
            # just changed will not.
            "projected_monthly_usd": round(cost / max(days, 1) * 30, 4),
            "peak_cost_usd": round(peak_cost or 0, 6),
            "daily": daily, "by_model": by_model, "by_provider": by_provider,
            "by_task": by_task, "rates": rates, "unpriced": unpriced,
            "lifetime": totals(con)}


def totals(con):
    """All-time and today's spend — the two numbers that answer "is this thing
    costing more than it used to" without picking a window first. All-time is
    the point of the table: it survives the restarts the session counters do not."""
    try:
        life = con.execute(
            "SELECT SUM(calls) calls, SUM(total_tokens) total_tokens,"
            " SUM(cost_usd) cost_usd, MIN(first_at) since FROM llm_usage").fetchone()
        today = con.execute(
            "SELECT SUM(calls) calls, SUM(total_tokens) total_tokens,"
            " SUM(cost_usd) cost_usd FROM llm_usage WHERE day = ?",
            (analytics.day_key(),)).fetchone()
    except sqlite3.Error:
        return {}
    return {"calls": life["calls"] or 0,
            "total_tokens": life["total_tokens"] or 0,
            "cost_usd": round(life["cost_usd"] or 0, 6),
            "tracking_since": life["since"],
            "today": {"calls": today["calls"] or 0,
                      "total_tokens": today["total_tokens"] or 0,
                      "cost_usd": round(today["cost_usd"] or 0, 6)}}
