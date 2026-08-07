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
