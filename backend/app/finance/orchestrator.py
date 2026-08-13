"""Finance pipeline runner.

Structurally identical to app/orchestrator.py — same run-id tagging, same
per-stage checkpoints and mem= readings, same "a stage that raises is logged
and the run continues", same gc/prune at the end. That is on purpose: whichever
pipeline is in the log, `run=<id> stage=X start` with no matching `done` means
the same thing, and the OOM-diagnosis habit built on recent_runs works on both
without anyone learning a second convention.

Deliberately NOT shared with the general runner. Merging them would mean a
finance stage failure could abort the news pipeline, and the two are supposed
to be isolated.
"""
import gc
import time
import uuid

from .. import config, db, diag, fulltext, llm
from ..agents import Verifier
from . import kg
from .agents import (FinancialForecastingAgent, FinancialStoryAgent,
                     FinancialTrendAgent)

STAGES = ["fin_stories", "fin_trends", "fin_forecasts"]


def run_finance_pipeline(stage=None):
    """Run the finance stages in order. Ingestion is NOT one of them: Scout and
    Deduper already fetched and grouped these articles for the general
    pipeline, and fetching the same feeds twice would double the request load
    on publishers for no gain."""
    con = db.connect()
    stages = [stage] if stage else STAGES
    results = {}
    run_id = uuid.uuid4().hex[:8]
    diag.checkpoint(f"run={run_id} finance pipeline start stages={stages}")
    verifier = Verifier()
    for s in stages:
        t0 = time.time()
        diag.checkpoint(f"run={run_id} stage={s} start")
        llm.set_context(f"run={run_id} stage={s}")
        try:
            if s == "fin_stories":
                results[s] = FinancialStoryAgent().run(con, verifier)
            elif s == "fin_trends":
                results[s] = FinancialTrendAgent().run(con)
            elif s == "fin_forecasts":
                results[s] = FinancialForecastingAgent().run(con)
        except Exception as e:  # noqa: BLE001
            db.log_run(con, s, "error", str(e)[:300])
            results[s] = f"error: {e}"
        diag.checkpoint(f"run={run_id} stage={s} done dur={time.time()-t0:.1f}s "
                        f"result={results.get(s)}")
    # The graph is the one table here that only ever grows, so it gets its
    # ceiling enforced on every run rather than on a separate timer that could
    # quietly stop firing.
    try:
        edges, nodes = kg.prune(con)
        if edges or nodes:
            db.log_run(con, "fin_kg", "ok", f"pruned {edges} edges, {nodes} nodes")
        con.commit()
    except Exception as e:                       # never fail a run over cleanup
        db.log_run(con, "fin_kg", "warn", f"prune failed: {e}")
    db.log_run(con, "finance_pipeline", "done", str(results),
               llm_calls=llm.usage["calls"], llm_tokens=llm.usage["tokens"])
    con.close()
    fulltext.prune_stale_hosts()
    gc.collect()
    diag.checkpoint(f"run={run_id} finance pipeline done")
    llm.set_context("")
    return results


#: Stage names this pipeline writes to `runs`, in the order they execute.
_RUN_STAGES = ("finance_pipeline",) + tuple(STAGES)


def health(con):
    """Answer one question: is the finance pipeline actually working?

    It exists because "no finance stories changed" has four completely different
    causes that look identical from the outside:

      1. nothing ever runs it — with FINANCE_IN_PROCESS=0 the API schedules
         NOTHING, so unless a separate worker/cron invokes
         run_finance_pipeline.py the pipeline has simply never executed;
      2. it runs but has no input — no articles on FINANCE_TOPICS;
      3. it runs and fails — a stage error, visible only as one row among
         thirty in recent_runs, where the news pipeline's own rows crowd it out;
      4. it runs fine and there is genuinely no new finance news.

    Empty endpoints look the same in all four. So this reports the evidence for
    each, and names the ones that are actually wrong in `problems`."""
    now = db.now()

    def last_run(stage):
        r = con.execute(
            "SELECT status, detail, created_at, llm_calls, llm_tokens FROM runs "
            "WHERE stage=? ORDER BY created_at DESC LIMIT 1", (stage,)).fetchone()
        if not r:
            return None
        return {"at": r["created_at"],
                "ago_hours": round((now - (r["created_at"] or now)) / 3600, 1),
                "status": r["status"], "detail": (r["detail"] or "")[:300],
                "llm_calls": r["llm_calls"], "llm_tokens": r["llm_tokens"]}

    def table(name, ts="updated_at"):
        row = con.execute(
            f"SELECT COUNT(*) n, MAX({ts}) newest FROM {name}").fetchone()
        newest = row["newest"]
        return {"rows": row["n"] or 0, "newest_at": newest,
                "newest_age_hours": round((now - newest) / 3600, 1) if newest else None}

    runs = {s: last_run(s) for s in _RUN_STAGES}
    content = {"fin_stories": table("fin_stories"),
               "fin_trends": table("fin_trends"),
               "fin_forecasts": table("fin_forecasts")}
    graph = kg.stats(con)

    topics = config.FINANCE_TOPICS
    placeholders = ",".join("?" * len(topics)) if topics else "''"
    eligible = con.execute(
        f"SELECT COUNT(*) n FROM articles WHERE LOWER(topic) IN ({placeholders}) "
        "AND fetched_at > ?",
        (*topics, now - config.FIN_WINDOW_DAYS * 86400)).fetchone()["n"] if topics else 0

    # Chained runs inherit the NEWS cadence, not FINANCE_INTERVAL_HOURS — that
    # setting is dead while chaining is on, and reporting staleness against it
    # would call a perfectly healthy 6-hourly pipeline late every single cycle.
    chained = config.FINANCE_AFTER_PIPELINE and bool(config.FINANCE_TOPICS)
    interval_h = (config.PIPELINE_INTERVAL_HOURS if chained
                  else config.FINANCE_INTERVAL_HOURS)
    if chained:
        runner = (f"chained after each full news run (every {interval_h:g}h), "
                  "gated on " + (",".join(config.FINANCE_REQUIRED_STAGES)
                                 if config.FINANCE_REQUIRED_STAGES else "all stages"))
    elif config.FINANCE_IN_PROCESS:
        runner = f"API scheduler, every {interval_h:g}h"
    else:
        runner = "external worker/cron: python run_finance_pipeline.py"
    # Two missed cycles before calling it stale — one late run is a slow cron,
    # not a broken one.
    stale_after = interval_h * 2
    pipe = runs.get("finance_pipeline")
    problems, verdict = [], "healthy"

    if not topics:
        problems.append("FINANCE_TOPICS is empty — the finance pipeline is "
                        "switched off and will produce nothing.")
        verdict = "disabled"
    elif pipe is None:
        verdict = "never_run"
        problems.append(
            "The finance pipeline has NEVER run on this database — there is no "
            "'finance_pipeline' row in `runs`. "
            + ("It is chained to the news pipeline, so it runs at the END of the "
               f"next full news run (within {interval_h:g}h). If one has "
               "completed since deploy and this is still empty, check that the "
               "news run finished every stage — a chained run that was gated "
               "logs status='skipped' here."
               if chained else
               "FINANCE_IN_PROCESS=0, so the API process deliberately schedules "
               "nothing: something external must run `python "
               "run_finance_pipeline.py` on a timer. If no cron/worker exists, "
               "that is the whole explanation."
               if not config.FINANCE_IN_PROCESS else
               "FINANCE_IN_PROCESS=1, so the API should be scheduling it — check "
               "the startup log for 'finance pipeline scheduled IN-PROCESS'."))
    elif pipe["status"] == "skipped":
        verdict = "gated"
        problems.append(
            "The last chained finance run was SKIPPED because the news run "
            f"before it failed: {pipe['detail'][:200]}")
    elif pipe["ago_hours"] > stale_after:
        verdict = "stale"
        problems.append(
            f"Last finance run was {pipe['ago_hours']}h ago, but the interval is "
            f"{interval_h}h — it has missed at least two cycles. Whatever "
            f"triggers it has stopped.")

    for s in STAGES:
        r = runs.get(s)
        if r and r["status"] == "error":
            verdict = "failing" if verdict in ("healthy", "stale") else verdict
            problems.append(f"Stage {s} last ended in error: {r['detail'][:160]}")

    if topics and not eligible:
        problems.append(
            f"No articles on topics {topics} in the last "
            f"{config.FIN_WINDOW_DAYS:g} days — the pipeline has nothing to read. "
            f"Check that feeds.yaml actually assigns those topic names.")
        if verdict == "healthy":
            verdict = "no_input"

    if (verdict == "healthy" and pipe and not content["fin_stories"]["rows"]):
        verdict = "running_but_empty"
        problems.append(
            "The pipeline ran but fin_stories is empty. Check the fin_stories "
            "stage detail below and /admin/finance/unresolved.")

    return {"verdict": verdict, "problems": problems,
            "enabled": bool(topics), "topics": topics,
            "scheduling": {
                "chained_after_news": chained,
                "in_process": config.FINANCE_IN_PROCESS,
                "interval_hours": interval_h,
                "required_stages": config.FINANCE_REQUIRED_STAGES or "all",
                "runner": runner,
                "considered_stale_after_hours": stale_after},
            "last_runs": runs, "content": content, "graph": graph,
            "input": {"eligible_articles": eligible,
                      "window_days": config.FIN_WINDOW_DAYS,
                      "max_stories_per_run": config.FIN_MAX_STORIES_PER_RUN}}


def unresolved_report(con, limit=40):
    """Company names the pipeline saw but tickers.yaml could not resolve, most
    frequent first. This is the work queue for extending the map — the point of
    recording unresolved names rather than dropping them."""
    counts = {}
    for r in con.execute(
            "SELECT unresolved FROM fin_stories WHERE updated_at > ?",
            (db.now() - config.FIN_TREND_WINDOW_DAYS * 86400,)).fetchall():
        for name in db.uj(r["unresolved"], []):
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
