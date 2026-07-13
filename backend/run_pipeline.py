"""Run the pipeline from the command line.

  python run_pipeline.py               # all stages
  python run_pipeline.py --stage trends
"""
import argparse
from app.orchestrator import run_pipeline, STAGES

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=STAGES, default=None)
    args = ap.parse_args()
    results = run_pipeline(args.stage)
    for k, v in results.items():
        print(f"{k:14} {v}")
