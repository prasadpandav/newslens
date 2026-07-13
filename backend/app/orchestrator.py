"""Planner + Runner. Plans which topics/stages to run, executes the DAG,
logs every stage. Re-runnable: each stage skips work already done."""
from . import db, llm
from .agents import (Scout, EntityTagger, TrendLinker, MicroTrendDetector,
                     ConnectionFinder, Verifier, Storyteller, Personalizer)

STAGES = ["scout", "entities", "trends", "micro_trends", "connections",
          "stories", "personalize"]


def plan(con):
    """Planner: derive topics from the union of user interests + baseline."""
    topics = {"world", "business", "technology"}
    for u in con.execute("SELECT context FROM users").fetchall():
        ctx = db.uj(u["context"])
        topics.update(t.lower() for t in ctx.get("interests", []))
        country = str(ctx.get("location", {}).get("country", "")).lower()
        if country:
            topics.add(country)
    return {"topics": topics, "stages": STAGES}


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
            elif s == "personalize":
                results[s] = Personalizer().run(con)
        except Exception as e:  # noqa: BLE001
            db.log_run(con, s, "error", str(e)[:300])
            results[s] = f"error: {e}"
    db.log_run(con, "pipeline", "done", str(results),
               llm_calls=llm.usage["calls"], llm_tokens=llm.usage["tokens"])
    con.close()
    return results
