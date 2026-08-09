"""Planner + Runner. Plans which topics/stages to run, executes the DAG,
logs every stage. Re-runnable: each stage skips work already done."""
import gc
import time
import uuid
from . import db, diag, fulltext, gazetteer, llm, llmcache
from .agents import (Scout, Deduper, EntityTagger, TrendLinker, MicroTrendDetector,
                     ConnectionFinder, Verifier, Storyteller, Foresight)

# micro_trends is retired as a separate stage — it's folded into the per-unit
# "trends" call (each topic call returns both macro and emerging/micro trends).
# "dedupe" groups near-duplicate articles right after scout so the LLM stages
# process each event once (with sources annotated) instead of once per source.
# No "personalize" stage: personalization is on-demand now (Personalizer.
# personalize, called from POST /story/{id}/personalize when a reader actually
# opens "What this means for you"), not a batch LLM call over every user x
# every story on every run — that was the majority of this pipeline's LLM
# spend for text most readers never opened.
STAGES = ["scout", "dedupe", "entities", "trends", "connections",
          "stories", "signals"]


def plan(con):
    """Planner: fetch ALL configured topics — users can always browse everything.
    Interests influence personalization and ranking, never availability."""
    return {"topics": None, "stages": STAGES}  # None = every topic in feeds.yaml


def run_pipeline(stage=None):
    con = db.connect()
    p = plan(con)
    stages = [stage] if stage else p["stages"]
    verifier = Verifier()
    results = {}
    # Short id tagging every log line from this run, so an LLM warning like
    # "rate_limited | provider=groq task=entities" can be tied back to WHICH
    # run and stage produced it — the thing the bare warnings can't say on
    # their own. Paired with a mem= reading at every stage boundary: if the
    # process gets SIGKILLed (no traceback, no exception, nothing) the last
    # "run=... stage=X start" line with no matching "done" tells you exactly
    # which stage was in progress and how big the process had gotten.
    run_id = uuid.uuid4().hex[:8]
    diag.checkpoint(f"run={run_id} pipeline start stages={stages}")
    for s in stages:
        t0 = time.time()
        diag.checkpoint(f"run={run_id} stage={s} start")
        llm.set_context(f"run={run_id} stage={s}")
        try:
            if s == "scout":
                results[s] = Scout().run(con, topics=p["topics"])
            elif s == "dedupe":
                results[s] = Deduper().run(con)
            elif s == "entities":
                results[s] = EntityTagger().run(con)
            elif s == "trends":
                results[s] = TrendLinker().run(con)
            elif s == "micro_trends":
                results[s] = MicroTrendDetector().run(con)
            elif s == "connections":
                results[s] = ConnectionFinder().run(con)
            elif s == "stories":
                results[s] = Storyteller().run(con, verifier)
            elif s == "signals":
                results[s] = Foresight().run(con)
        except Exception as e:  # noqa: BLE001
            db.log_run(con, s, "error", str(e)[:300])
            results[s] = f"error: {e}"
        diag.checkpoint(f"run={run_id} stage={s} done dur={time.time()-t0:.1f}s "
                        f"result={results.get(s)}")
    # story_history only ever grows (Storyteller appends a row whenever a
    # story's corroboration actually moves), so it needs a ceiling. Done here,
    # once per run, while we still hold the connection.
    try:
        dropped = db.prune_history(con)
        if dropped:
            db.log_run(con, "history", "ok", f"pruned {dropped} stale history rows")
    except Exception as e:                       # never fail a run over cleanup
        db.log_run(con, "history", "warn", f"prune failed: {e}")
    # The answer cache only IGNORES expired rows on read, so its sweep lives
    # here — once per run, next to the other bounded-growth cleanups, rather
    # than on the request path.
    try:
        expired = llmcache.purge(con)
        if expired:
            db.log_run(con, "llm_cache", "ok", f"purged {expired} expired answers")
    except Exception as e:                       # never fail a run over cleanup
        db.log_run(con, "llm_cache", "warn", f"purge failed: {e}")
    # Same reasoning: the gazetteer grows with the news, and the terms seen once
    # and never again are the ones that would crowd out the recurring vocabulary
    # under load()'s cap.
    try:
        stale = gazetteer.prune(con)
        if stale:
            db.log_run(con, "gazetteer", "ok", f"pruned {stale} one-off terms")
    except Exception as e:                       # never fail a run over cleanup
        db.log_run(con, "gazetteer", "warn", f"prune failed: {e}")
    db.log_run(con, "pipeline", "done", str(results),
               llm_calls=llm.usage["calls"], llm_tokens=llm.usage["tokens"])
    con.close()
    # Every stage above is its own short-lived object whose locals (fetched
    # rows, merged briefs, per-story text) are already released the moment
    # each .run() returns — normal refcounting, nothing special needed. What
    # DOESN'T self-clean is fulltext's module-level host cache, which this
    # run may have just added entries to; prune it here rather than on a
    # separate timer, and force a collection so any reference cycles built up
    # over 8 stages don't linger in a worker thread the scheduler reuses for
    # the next job. Cheap (single-digit ms) and safe — this is a background
    # thread, so it costs nothing in request latency. See render-512mb-oom-limit.
    fulltext.prune_stale_hosts()
    gc.collect()
    diag.checkpoint(f"run={run_id} pipeline done")
    llm.set_context("")   # this thread is reused by the scheduler — don't let
                          # a stale run/stage label leak onto the next job
    return results
