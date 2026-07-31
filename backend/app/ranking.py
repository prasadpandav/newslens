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

Developing items get *bounded* credit for being retold. A long-running
storyline that picks up one new article per run genuinely is fresher than an
abandoned one, so age is measured from the last retell (`updated_at`) — but
only down to `STALE_FLOOR` of the item's true age since first told. Without
that floor a retell resets age to zero, and an item that keeps accreting a
trickle of articles parks at rank #1 forever, which is precisely what this
module is supposed to prevent. With it, a week-old storyline retold an hour
ago still ranks as several cycles old and has to earn the top slot on impact.
"""
import math
import time
from . import config

#: An item can never present as fresher than this fraction of its true age.
STALE_FLOOR = 0.5


def damped(count):
    """Compress an unbounded count-style impact metric (article/source count,
    macro trend velocity) so it can't outgrow the recency term.

    A developing storyline accretes articles forever — production had a top
    story holding 231 of them — and raw counts enter the score linearly while
    age only ever divides it. Left undamped, such a storyline outranks genuinely
    new reporting no matter how correctly it is aged, which is the same "parked
    at the top" failure from the other direction. log1p keeps the metric
    monotonic, so ordering among items of equal age is completely unchanged;
    it only stops a big number from buying unlimited immunity to ageing.
    """
    return math.log1p(max(count, 0.0))


def effective_age(created_at, updated_at, now):
    """Seconds of age for ranking: time since the last retell, floored so it
    can't drop below STALE_FLOOR of the time since the item was first told."""
    born = created_at or now
    touched = max(updated_at or born, born)   # a retell predating creation is meaningless
    return max(0.0, max(now - touched, (now - born) * STALE_FLOOR))


def rank_score(impact, created_at, now=None, updated_at=None):
    """Higher is better. `impact` is whatever comparable-within-this-list metric
    the caller passes (source count, personalized impact_score, trend velocity,
    forecast confidence, ...) — this function only handles the recency half."""
    now = time.time() if now is None else now
    cycle_hours = max(config.PIPELINE_INTERVAL_HOURS, 0.5)  # guard div-by-zero
    age_cycles = effective_age(created_at, updated_at, now) / 3600.0 / cycle_hours
    return max(impact, 0.0) / (1.0 + age_cycles) ** config.RANK_GRAVITY


def sort_by_rank(rows, impact_of, created_at_of=lambda r: r.get("created_at"),
                 updated_at_of=lambda r: r.get("updated_at")):
    """Sort dict rows by rank_score, highest (best) first. One `now` for the
    whole list so relative order can't shift mid-sort."""
    now = time.time()
    return sorted(rows,
                  key=lambda r: rank_score(impact_of(r), created_at_of(r), now,
                                           updated_at_of(r)),
                  reverse=True)
