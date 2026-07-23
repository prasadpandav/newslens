"""Planner + Runner. Plans which topics/stages to run, executes the DAG,
logs every stage. Re-runnable: each stage skips work already done."""
from . import db, llm
from .agents import (Scout, Deduper, EntityTagger, TrendLinker, MicroTrendDetector,
                     ConnectionFinder, Verifier, Storyteller, Foresight,
                     Personalizer)

# micro_trends is retired as a separate stage — it's folded into the per-unit
# "trends" call (each topic call returns both macro and emerging/micro trends).
# "dedupe" groups near-duplicate articles right after scout so the LLM stages
# process each event once (with sources annotated) instead of once per source.
STAGES = ["scout", "dedupe", "entities", "trends", "connections",
          "stories", "signals", "personalize"]


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
    for s in stages:
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
            elif s == "personalize":
                results[s] = Personalizer().run(con)
        except Exception as e:  # noqa: BLE001
            db.log_run(con, s, "error", str(e)[:300])
            results[s] = f"error: {e}"
    db.log_run(con, "pipeline", "done", str(results),
               llm_calls=llm.usage["calls"], llm_tokens=llm.usage["tokens"])
    con.close()
    return results
