"""Recency + impact ordering, shared by /feed, /stories, /trends and /signals.

Pure recency (created_at DESC) buries a big story the moment a trickle of
smaller ones outpaces it. Pure impact (velocity/confidence/source-count DESC)
has the opposite problem: a story that was once heavily corroborated can sit
at the top for days after coverage has moved on. Neither alone satisfies "the
most impactful thing from the latest run leads, but nothing stale parks at the
top" — so every list here is ordered by one blended score instead:

    score = impact / (1 + age_in_cycles) ** RANK_GRAVITY

"age_in_cycles" is age measured in PIPELINE_INTERVAL_HOURS-sized units rather
than raw hours, so the curve adapts automatically if the pipeline cadence
changes: something touched in the run that just finished (age ~0 cycles) is
barely discounted at all, so it takes real impact for an older item to outrank
it; one run back it's already divided by 2**GRAVITY; a handful of runs back
the discount is steep; and anything on the order of the multi-day retention
window is discounted to near zero no matter how impactful it once was — which
is what keeps stale items from ever parking at the top.
"""
import time
from . import config


def rank_score(impact, created_at, now=None):
    """Higher is better. `impact` is whatever comparable-within-this-list metric
    the caller passes (source count, personalized impact_score, trend velocity,
    forecast confidence, ...) — this function only handles the recency half."""
    now = time.time() if now is None else now
    cycle_hours = max(config.PIPELINE_INTERVAL_HOURS, 0.5)  # guard div-by-zero
    age_cycles = max(0.0, now - (created_at or now)) / 3600.0 / cycle_hours
    return max(impact, 0.0) / (1.0 + age_cycles) ** config.RANK_GRAVITY


def sort_by_rank(rows, impact_of, created_at_of=lambda r: r.get("created_at")):
    """Sort dict rows by rank_score, highest (best) first. One `now` for the
    whole list so relative order can't shift mid-sort."""
    now = time.time()
    return sorted(rows, key=lambda r: rank_score(impact_of(r), created_at_of(r), now),
                  reverse=True)
