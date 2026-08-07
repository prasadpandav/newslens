"""Run the finance pipeline from the command line — the way it is meant to run
in production, as its own worker/cron process rather than inside the API.

  python run_finance_pipeline.py                  # all stages
  python run_finance_pipeline.py --stage fin_trends
  python run_finance_pipeline.py --unresolved     # names tickers.yaml is missing
  python run_finance_pipeline.py --graph          # knowledge graph size + hubs
"""
import argparse

from app import db
from app.finance import kg
from app.finance.orchestrator import STAGES, run_finance_pipeline, unresolved_report

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=STAGES, default=None)
    ap.add_argument("--unresolved", action="store_true",
                    help="report company names that tickers.yaml could not resolve")
    ap.add_argument("--graph", action="store_true",
                    help="report knowledge graph size and its most connected nodes")
    args = ap.parse_args()

    if args.unresolved or args.graph:
        con = db.connect()
        if args.unresolved:
            rows = unresolved_report(con)
            print(f"{len(rows)} unresolved name(s) — add the real ones to tickers.yaml:")
            for name, n in rows:
                print(f"  {n:4}  {name}")
        if args.graph:
            s = kg.stats(con)
            print(f"nodes {s['nodes']}  edges {s['edges']}")
            for h in s["hubs"]:
                print(f"  hub  {h['degree']:4}  {h['node']}")
        con.close()
    else:
        for k, v in run_finance_pipeline(args.stage).items():
            print(f"{k:14} {v}")
