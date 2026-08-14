"""Provider-agnostic LLM client.

Providers: mock (no keys, heuristic answers so the whole pipeline runs),
groq, gemini, auto (groq first, gemini on rate-limit/failure).
Every call asks for JSON and validates it; one retry with the error appended.

Usage is recorded twice, on purpose. `usage` below is a process-local live view
for /admin/usage — what THIS process has done since it started. It is also
wired through to app/llmcost.py, which writes the same call to SQLite broken
down by provider, model and task, because this dict is lost on every restart
and the finance question ("what did last month cost") outlives the process.
"""
import json
import os
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
import httpx
from . import config, llmcache, llmcost

usage = {"calls": 0, "tokens": 0, "provider_calls": {}, "provider_attempts": {},
         "model_calls": {}, "model_tokens": {}, "cost_usd": 0.0,
         "rate_limited": 0, "failed": 0, "throttle_waits": 0,
         "cache_hits": 0, "budget_blocked": 0}

#: How many times one complete_json call may wait on a full per-minute window
#: before giving up. Each pass sleeps at most 60s, so this bounds a throttled
#: call at ~3 minutes rather than letting a misconfigured cap stall a stage.
MAX_THROTTLE_PASSES = 3

# ------------------------------------------------------------ observability
log = logging.getLogger("newslens.llm")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                      "%H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

recent_errors: deque = deque(maxlen=50)   # exposed at /admin/usage
# Rate-limit/bench events are rare but crucial — keep them separately so a
# flood of gave_up entries can never push them out of view.
provider_events: deque = deque(maxlen=20)

# What's calling the LLM right now, on THIS thread. A bare "rate_limited |
# provider=groq task=entities" doesn't say whether that came from the
# scheduled pipeline, a manual admin trigger, or a signed-in reader's
# on-demand personalize request — which was exactly the gap when diagnosing
# the memory climb, since the crash itself leaves no traceback. thread-local
# because the scheduled pipeline (one background thread) and concurrent
# request handlers (FastAPI's threadpool, one thread per sync request) call
# complete_json at the same time; a plain module global would let one
# overwrite the other's context mid-call.
_context = threading.local()


def set_context(label):
    """Tag every complete_json call on this thread until cleared. Call at the
    start of a pipeline stage (orchestrator.py) or an on-demand request
    (main.py's personalize endpoint) with something identifying enough to
    grep for later, e.g. 'run=ab12cd stage=entities' or
    'request personalize story=xyz user=abc'."""
    _context.label = label


def _current_context():
    return getattr(_context, "label", "")


def provider_status():
    """Live view: which providers are benched and for how much longer."""
    now = time.time()
    return {p: {"benched": now < until,
                "benched_for_seconds": max(0, int(until - now)),
                "consecutive_rate_limits": _bench_strikes.get(p, 0)}
            for p, until in _benched_until.items()}


def routing_status():
    """Which model each tier resolves to, and how much of the daily ceiling is
    gone. This is the answer to "why is task X landing on that model" — the
    question the old flat provider order gave no way to ask, and the reason an
    entity extraction could be served by a frontier model unnoticed."""
    spend, calls = llmcost.today_usage()
    probe = {"cheap": "entities", "standard": "story", "reasoning": "trend"}
    tiers = {}
    for tier, task in probe.items():
        tiers[tier] = {p: _model_for(p, task)
                       for p in ("groq", "gemini", "deepseek", "openai")
                       if _has_key(p)}
    budget = config.LLM_DAILY_BUDGET_USD
    return {"tier_models": tiers,
            "warnings": unpaced_models(),
            "free_providers": sorted(config.FREE_PROVIDERS),
            # Paid only — free-tier traffic is priced in the report for
            # comparison but is not money. See llmcost.today_usage.
            "today_paid_spend_usd": round(spend, 6),
            "today_calls_by_provider": calls,
            "daily_budget_usd": budget,
            "budget_exhausted": bool(budget > 0 and spend >= budget),
            "daily_call_limits": config.PROVIDER_DAILY_CALL_LIMITS,
            "per_minute_caps": config.PROVIDER_MAX_CALLS_PER_MIN,
            "global_per_minute_cap": config.LLM_MAX_CALLS_PER_MIN}


def _note(kind, provider, task, msg):
    """One short line per failure: what failed, where, why, and what was
    running — see _current_context()."""
    msg = str(msg)[:160]
    ctx = _current_context()
    log.warning("%s | provider=%s task=%s%s | %s", kind, provider, task,
               f" | {ctx}" if ctx else "", msg)
    recent_errors.appendleft({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind, "provider": provider, "task": task, "context": ctx, "detail": msg})

# Pace calls to stay under free-tier requests-per-minute limits.
# Keys are (provider, model) tuples; provider-only fallback for unknown models.
_last_call: dict[tuple | str, float] = {}
MIN_INTERVAL = {
    ("groq", None): 2.1,
    # Gemini models have different free-tier RPM limits. Listed per model, but
    # resolved by FAMILY too — see _interval_for. Naming a model here that is
    # one character off the one actually configured (gemini-3.5-flash-lite vs
    # the deployed gemini-3.1-flash-lite) silently removed all pacing, which is
    # why the family fallback exists rather than a longer list.
    ("gemini", "gemini-2.5-flash-lite"): 4.3,  # 15 RPM limit → 14 calls/min
    ("gemini", "gemini-3.5-flash-lite"): 4.3,  # 15 RPM limit → 14 calls/min
    ("gemini", "gemini-2.5-flash"): 6.7,       # 10 RPM limit → 9 calls/min
    ("gemini", "gemini-3-flash"): 6.7,         # 10 RPM limit → 9 calls/min
    # Fail-safe for any Gemini model not named above: the slowest known limit.
    # Without it the lookup returned 0 and an unrecognised model was sent at the
    # global cap — straight into a 429 and a bench, every run.
    ("gemini", None): 6.7,
    ("deepseek", None): 0.5,
    ("openai", None): 0.5,
}

#: Suffix -> interval, applied when the exact model is not in MIN_INTERVAL.
#: Google's tiers track the family name, so a new point release inherits its
#: family's limit instead of inheriting no limit at all.
_FAMILY_INTERVAL = (("flash-lite", 4.3), ("flash", 6.7))


def _interval_for(provider, model):
    """Minimum seconds between calls for this provider+model.

    Exact model first, then family, then the provider default. The order matters:
    an operator who names a specific model is overriding the family, and a
    provider default that is too slow is a throughput problem, while one that is
    too fast is a rate-limit ban."""
    if (provider, model) in MIN_INTERVAL:
        return MIN_INTERVAL[(provider, model)]
    name = str(model or "").lower()
    for suffix, interval in _FAMILY_INTERVAL:
        if (provider, None) in MIN_INTERVAL and name.endswith(suffix):
            return max(interval, 0)
    return MIN_INTERVAL.get((provider, None), 0)


def unpaced_models():
    """Configured models that would be sent at the global cap, and priced models
    that would be costed at $0. Surfaced at startup and on /admin/usage: both
    failures are silent, and both were live in production."""
    out = {"unpaced": [], "unpriced": []}
    for p in ("groq", "gemini", "deepseek", "openai"):
        if not _has_key(p):
            continue
        for task in ("entities", "story", "trend"):
            model = _model_for(p, task)
            if not model:
                continue
            entry = f"{p}/{model}"
            if not _interval_for(p, model) and entry not in out["unpaced"]:
                out["unpaced"].append(entry)
            _, source = llmcost.price_for(p, model)
            if source == "unpriced" and entry not in out["unpriced"]:
                out["unpriced"].append(entry)
    return out

# When a provider rate-limits us, bench it for a while instead of knocking on
# its door for every call. It gets retried automatically after the cooldown.
_benched_until: dict[str, float] = {}
COOLDOWN_SECONDS = 900  # 15 min

#: Consecutive rate-limits per provider, reset by its next success. A flat 15
#: minute bench is right for a per-MINUTE limit, which clears by itself, and
#: wrong for a per-DAY allowance, which does not: it re-probes an exhausted quota
#: four times an hour until midnight, and every probe pushes the run onto the
#: paid provider. Doubling the bench per strike turns that into a handful of
#: probes a day; the first success clears the count, so a genuine per-minute
#: blip still recovers in 15 minutes.
_bench_strikes: dict[str, int] = {}


def _bench(provider, seconds=None):
    """Bench `provider`, doubling the cooldown for repeat offenders."""
    strikes = _bench_strikes[provider] = _bench_strikes.get(provider, 0) + 1
    if seconds is None:
        seconds = min(COOLDOWN_SECONDS * (2 ** (strikes - 1)),
                      config.PROVIDER_BENCH_MAX_SECONDS)
    _benched_until[provider] = time.time() + seconds
    return int(seconds)
# A provider that is out of credit, or whose key was revoked, is benched for
# much longer than a rate-limited one: a 429 clears by itself, a 402 waits for a
# human to top up. 15 minutes would mean re-discovering an empty account four
# times an hour, once per pipeline stage that uses it. Still finite, so topping
# up is picked up on its own without a redeploy.
PROVIDER_DOWN_COOLDOWN_SECONDS = int(
    os.environ.get("PROVIDER_DOWN_COOLDOWN_SECONDS", str(60 * 60)))

# Sliding-window throttles: ≤ LLM_MAX_CALLS_PER_MIN real calls in any 60s across
# ALL tasks/providers/threads, and ≤ PROVIDER_MAX_CALLS_PER_MIN[p] for each
# provider within that. Guards free-tier RPM and caps token burn.
_call_times: deque = deque()
_provider_call_times: dict[str, deque] = {}
_throttle_lock = threading.Lock()


def _prune(times, now):
    while times and now - times[0] >= 60:
        times.popleft()


def _try_reserve(provider):
    """Reserve one call slot for `provider`. Returns 0.0 when reserved, else the
    seconds until the tightest of the two windows frees up.

    Non-blocking on purpose. The caller can then offer the call to the NEXT
    provider in the order instead of waiting — which is the whole point of the
    per-provider slice, since blocking here is what let one provider hold the
    entire global budget while the others sat idle."""
    with _throttle_lock:
        now = time.time()
        waits = []
        g_limit = config.LLM_MAX_CALLS_PER_MIN
        _prune(_call_times, now)
        if g_limit > 0 and len(_call_times) >= g_limit:
            waits.append(60 - (now - _call_times[0]) + 0.01)
        p_limit = config.PROVIDER_MAX_CALLS_PER_MIN.get(provider, 0)
        p_times = _provider_call_times.setdefault(provider, deque())
        _prune(p_times, now)
        if p_limit > 0 and len(p_times) >= p_limit:
            waits.append(60 - (now - p_times[0]) + 0.01)
        if waits:
            return max(max(waits), 0.0)
        _call_times.append(now)
        p_times.append(now)
        return 0.0


def _pace(provider, model=None):
    key = (provider, model)
    interval = _interval_for(provider, model)
    wait = interval - (time.time() - _last_call.get(key, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[key] = time.time()


def _has_key(provider):
    return bool({"groq": config.GROQ_API_KEY, "gemini": config.GEMINI_API_KEY,
                 "deepseek": config.DEEPSEEK_API_KEY,
                 "openai": config.OPENAI_API_KEY}.get(provider, ""))


def deepseek_in_peak(now=None):
    """True if `now` (UTC) falls in a DeepSeek peak-pricing window (2x). Windows are
    half-open [start, end) hour ranges — e.g. 03:59 is peak, 04:00 is not."""
    if now is None:
        now = datetime.now(timezone.utc)
    h = now.hour
    return any(start <= h < end for start, end in config.DEEPSEEK_PEAK_WINDOWS_UTC)


def _apply_peak_order(order):
    """During DeepSeek peak hours, demote deepseek to the back so free providers run
    first and the 2x-priced provider is only a last resort. No-op off-peak, when the
    toggle is off, or when deepseek isn't in the running order anyway."""
    if (config.DEEPSEEK_AVOID_PEAK and "deepseek" in order
            and len(order) > 1 and deepseek_in_peak()):
        return [p for p in order if p != "deepseek"] + ["deepseek"]
    return order


def pricing_status():
    """DeepSeek peak state for /admin/usage — is it peak now, are we avoiding it,
    and the configured windows."""
    return {"deepseek_peak_now": deepseek_in_peak(),
            "deepseek_avoid_peak": config.DEEPSEEK_AVOID_PEAK,
            "peak_windows_utc": config.DEEPSEEK_PEAK_WINDOWS_UTC}


def _model_for(provider, task):
    """Pick the model for this provider+task, by the task's TIER.

    reasoning — the provider's stronger 'thinking' model, so trend and forecast
                synthesis reason more deeply.
    cheap     — the provider's small model. This is a ceiling, not a preference:
                a mechanical extraction must not be answerable by a frontier
                model just because it happened to be that provider's base.
    standard  — the base model, i.e. the behaviour every task had before tiers.

    Each falls back to the base model when its tier's model is unset."""
    tiers = {
        "groq": (config.GROQ_CHEAP_MODEL, config.GROQ_MODEL,
                 config.GROQ_REASONING_MODEL),
        "gemini": (config.GEMINI_CHEAP_MODEL, config.GEMINI_MODEL,
                   config.GEMINI_REASONING_MODEL),
        "deepseek": (config.DEEPSEEK_CHEAP_MODEL, config.DEEPSEEK_MODEL,
                     config.DEEPSEEK_REASONING_MODEL),
        "openai": (config.OPENAI_CHEAP_MODEL, config.OPENAI_MODEL,
                   config.OPENAI_REASONING_MODEL),
    }
    cheap, base, reason = tiers.get(provider, ("", "", ""))
    tier = config.tier_of(task)
    if tier == "reasoning":
        return reason or base
    if tier == "cheap":
        return cheap or base
    return base


def _is_paid(provider, task):
    """True when this provider would bill us for this task's model. Derived from
    the rate card rather than a hardcoded list, so a provider that becomes paid
    (or a model priced for the first time) is picked up without a code change.
    An UNPRICED model reads as paid: an unknown price is not a free one, and
    treating it as free is how an unpriced model escapes every ceiling below."""
    if provider == "mock" or provider in config.FREE_PROVIDERS:
        return False
    rate, source = llmcost.price_for(provider, _model_for(provider, task))
    if source == "unpriced":
        return True
    return any(r > 0 for r in rate)


def _affordable(order, task):
    """Drop providers we should not spend on right now: over the daily dollar
    budget, or past their own daily call allowance.

    Returns (order, notes). An empty order means every remaining provider is
    priced out — complete_json then returns None and the caller retries the item
    on the next run, which is the same path a total provider outage takes."""
    spend, calls = llmcost.today_usage()
    notes = []
    budget = config.LLM_DAILY_BUDGET_USD
    over_budget = budget > 0 and spend >= budget
    kept = []
    for p in order:
        limit = config.PROVIDER_DAILY_CALL_LIMITS.get(p, 0)
        if limit and calls.get(p, 0) >= limit:
            notes.append(f"{p}: daily call limit {limit} reached")
            continue
        if over_budget and _is_paid(p, task):
            notes.append(f"{p}: daily budget ${budget:g} spent (${spend:.4f})")
            continue
        kept.append(p)
    return kept, notes


def complete_json(task: str, prompt: str, retries: int = 1):
    """Return parsed JSON from the LLM, or None if every provider failed.

    IMPORTANT: with a real provider configured, failure returns None and the
    caller skips that item (it is retried on the next pipeline run). Mock
    content is only ever produced when LLM_PROVIDER=mock.
    """
    provider = config.LLM_PROVIDER
    if provider == "mock":
        return _mock(task, prompt)
    cached = llmcache.get(task, prompt)
    if cached is not None:
        usage["cache_hits"] += 1
        return cached
    if provider == "auto":
        # Each tier has its own running order: reasoning prefers the strongest
        # thinking model first (DeepSeek), cheap and standard keep the free-first
        # order. The tier also decides the MODEL each provider serves — see
        # _model_for — which is what stops a mechanical task reaching a frontier
        # model simply by falling through to the last provider in the list.
        tier = config.tier_of(task)
        base = (config.REASONING_PROVIDER_ORDER if tier == "reasoning"
                else config.CHEAP_PROVIDER_ORDER if tier == "cheap"
                else config.STANDARD_PROVIDER_ORDER)
        # During DeepSeek peak hours, demote it behind the free providers to dodge
        # the 2x charge (unless DEEPSEEK_AVOID_PEAK is off).
        order = _apply_peak_order([p for p in base if _has_key(p)])
    else:
        order = [provider]
    # Applied in BOTH modes. A daily ceiling that a pinned LLM_PROVIDER can walk
    # straight through is not a ceiling — and pinning one provider is exactly the
    # configuration with no fallback to absorb an overrun.
    order, priced_out = _affordable(order, task)
    if not order:
        usage["budget_blocked"] += 1
        _note("budget_blocked", "all", task, "; ".join(priced_out) or "no provider left")
        return None
    last_err = None
    attempt = 0
    # Passes spent waiting on a full per-minute window don't consume a retry —
    # a throttle is not a failed answer. Bounded separately so a misconfigured
    # cap can never spin here forever.
    throttle_passes = 0
    while attempt <= retries:
        deferred = []      # providers whose per-minute window is momentarily full
        for p in order:
            if time.time() < _benched_until.get(p, 0):
                continue  # provider is cooling down after a rate limit
            # Non-blocking: a provider at its per-minute slice is skipped so the
            # next one in the order can take the call. Only if EVERY provider is
            # full do we wait (below), and then only for the soonest slot.
            wait = _try_reserve(p)
            if wait:
                deferred.append(wait)
                continue
            try:
                usage["provider_attempts"][p] = usage["provider_attempts"].get(p, 0) + 1
                _attempt.recorded = False
                model = _model_for(p, task)
                _pace(p, model)
                text = _call(p, prompt if attempt == 0 else
                             f"{prompt}\n\nYour previous answer was invalid JSON ({last_err}). "
                             f"Reply with ONLY valid JSON.", task)
                _benched_until.pop(p, None)
                _bench_strikes.pop(p, None)   # it answered — forgive past 429s
                out = _extract_json(text)
                llmcache.put(task, prompt, out)
                return out
            except RateLimited:
                usage["rate_limited"] += 1
                _record_failure(p, task)
                secs = _bench(p)
                _note("rate_limited", p, task, f"429 — benched {secs // 60} min")
                provider_events.appendleft({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "provider": p, "event": "rate_limited_benched",
                    "benched_minutes": secs // 60, "task": task})
                continue
            except ProviderDown as e:
                # Bench it exactly like a rate limit. Nothing this run will make
                # a 402/401/403 succeed, and retrying it once per unit call is
                # how one dead provider turned into four identical failures and
                # ~75s of wasted pipeline time in a single stage. It comes back
                # off the bench after the cooldown, so topping up (or fixing the
                # key) is picked up without a redeploy.
                last_err = str(e)[:200]
                _record_failure(p, task)
                _bench(p, PROVIDER_DOWN_COOLDOWN_SECONDS)
                _note("provider_down", p, task,
                      f"{last_err} — benched {PROVIDER_DOWN_COOLDOWN_SECONDS // 60} min")
                provider_events.appendleft({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "provider": p, "event": f"down_{e.status}_benched",
                    "task": task})
                continue
            except json.JSONDecodeError as e:
                last_err = str(e)[:200]
                # Usually a no-op: the provider answered (and billed us), so
                # _record has already logged this attempt as a call with its
                # tokens, and _record_failure declines to count it twice. It is
                # still called because _extract_json can raise this before any
                # response was recorded when a provider returns an empty body.
                _record_failure(p, task)
                _note("invalid_json", p, task, last_err)
                continue
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:200]
                _record_failure(p, task)
                _note("error", p, task, last_err)
                continue
        # Nothing was attempted because every candidate was inside its
        # per-minute window. Wait for the soonest slot and re-run the order
        # WITHOUT spending a retry — otherwise the non-blocking reserve above
        # would turn a momentary throttle into a dropped item.
        if deferred and len(deferred) == len(order) and throttle_passes < MAX_THROTTLE_PASSES:
            throttle_passes += 1
            usage["throttle_waits"] += 1
            time.sleep(min(max(min(deferred), 0.0), 60))
            continue
        attempt += 1
    usage["failed"] += 1
    _note("gave_up", "all", task,
          last_err or f"providers {order} all unavailable or benched "
                      f"(LLM_PROVIDER={config.LLM_PROVIDER})")
    return None


class RateLimited(Exception):
    pass


class ProviderDown(Exception):
    """A provider failure that will NOT clear by trying again in a moment:
    no credit (402), a bad or revoked key (401), a blocked account (403).

    Distinct from RateLimited because the remedy is different — a rate limit
    clears on its own, this one waits for a human. Both bench the provider,
    which is the point: a DeepSeek balance of -$0.01 produced four identical
    402s in 75 seconds inside ONE trends stage, because every unit call retried
    the same dead provider before falling through. Benching it turns that into
    one failure and an immediate fallback."""

    def __init__(self, status, detail=""):
        super().__init__(f"{status}: {detail}"[:300])
        self.status = status


#: (provider, model) -> parameters that model has told us it will not accept.
#: Learned from its own 400s and remembered for the life of the process, so the
#: cost of discovering an incompatibility is one request, not one per call.
_unsupported: dict[tuple, set] = {}

#: The four shapes an OpenAI-compatible API uses to name the offending field.
_BAD_PARAM_RE = re.compile(
    r"Unsupported value: '([A-Za-z_]+)'"
    r"|Unsupported parameter: '([A-Za-z_]+)'"
    r"|Unrecognized request argument supplied: ([A-Za-z_]+)"
    r"|'([A-Za-z_]+)' is not supported")

#: Never strip these — without them there is no request left to make.
_ESSENTIAL = {"model", "messages"}


def _post_chat(url, key, body, timeout, provider, model):
    """POST to an OpenAI-compatible /chat/completions, adapting to the model.

    Models differ in which optional parameters they accept, and they say so with
    a 400 rather than by ignoring the field. `gpt-5.6-luna` rejects any
    `temperature` other than the default, which failed EVERY OpenAI call in the
    pipeline — and the log said only "Client error '400 Bad Request'", because
    httpx's message does not include the response body where the actual reason
    was. So: strip what this model has already refused, and when it refuses
    something new, learn it and retry once without it.
    """
    key_ = (provider, model)
    sent = {k: v for k, v in body.items() if k not in _unsupported.get(key_, ())}
    for _ in range(3):          # at most a couple of parameters to discover
        r = httpx.post(url, headers={"Authorization": f"Bearer {key}"},
                       json=sent, timeout=timeout)
        if r.status_code == 429:
            raise RateLimited()
        if r.status_code in (401, 402, 403):
            raise ProviderDown(r.status_code, r.text[:200])
        if r.status_code != 400:
            if r.status_code >= 400:
                # Carry the body: a bare status line is not a diagnosis.
                raise RuntimeError(f"{provider} {r.status_code}: {r.text[:300]}")
            return r
        detail = ""
        try:
            detail = (r.json().get("error") or {}).get("message") or ""
        except ValueError:
            detail = r.text[:300]
        m = _BAD_PARAM_RE.search(detail)
        param = next((g for g in (m.groups() if m else ()) if g), None)
        if not param or param in _ESSENTIAL or param not in sent:
            raise RuntimeError(f"{provider} 400: {detail[:300]}")
        _unsupported.setdefault(key_, set()).add(param)
        sent.pop(param, None)
        _note("param_dropped", provider, None,
              f"{model} rejected '{param}' — retrying without it")
    raise RuntimeError(f"{provider} 400: {detail[:300]}")


def _openai_usage(data):
    """(prompt, completion, cached, total) from an OpenAI-compatible response.

    `cached` is the slice of the prompt the provider served from its own cache
    and bills at a reduced rate. Only DeepSeek reports it; everywhere else it is
    absent and the whole prompt is charged at the full input rate."""
    u = data.get("usage") or {}
    p = int(u.get("prompt_tokens") or 0)
    c = int(u.get("completion_tokens") or 0)
    cached = int(u.get("prompt_cache_hit_tokens") or 0)
    if not cached:
        # OpenAI reports the same thing nested one level down.
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return p, c, cached, int(u.get("total_tokens") or 0) or (p + c)


def _gemini_usage(data):
    """(prompt, completion, cached, total) from a generateContent response.

    `candidatesTokenCount` counts only the answer, NOT the thinking tokens a
    reasoning model burns getting there — but those are billed as output. Adding
    them is the difference between a thinking model's real bill and a fraction
    of it."""
    u = data.get("usageMetadata") or {}
    p = int(u.get("promptTokenCount") or 0)
    c = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
    return (p, c, int(u.get("cachedContentTokenCount") or 0),
            int(u.get("totalTokenCount") or 0) or (p + c))


#: Whether the attempt running on THIS thread already reached _record. Set per
#: attempt in complete_json, so a failure handler can tell "the provider never
#: answered" (no tokens, count a failure) from "it answered and we could not use
#: the answer" (already counted as a billed call — counting it again would show
#: one attempt as both a call and a failure and make the failure rate a lie).
#: Thread-local for the same reason _context is: the scheduler's pipeline thread
#: and request handler threads are inside complete_json at the same time.
_attempt = threading.local()


def _record_failure(provider, task):
    """Count an attempt that cost us time and got nothing back. No-op when the
    call was already recorded as billed."""
    if getattr(_attempt, "recorded", False):
        return
    _record(provider, _model_for(provider, task), task, (0, 0, 0, 0), failed=True)


def _record(provider, model, task, tokens, latency_ms=0, failed=False):
    """One call's outcome, into the live counters AND into durable storage.

    `usage` is what /admin/usage reads for "this process"; llmcost writes the
    same call to SQLite keyed by day/provider/model/task so it survives the
    restart that empties this dict. `tokens` is (prompt, completion, cached,
    total) — the split matters because input and output are priced differently.
    """
    p, c, cached, total = tokens
    peak = provider == "deepseek" and deepseek_in_peak()
    _attempt.recorded = True
    if not failed:
        usage["calls"] += 1
        usage["tokens"] += total
        usage["provider_calls"][provider] = usage["provider_calls"].get(provider, 0) + 1
        key = f"{provider}/{model}"
        usage["model_calls"][key] = usage["model_calls"].get(key, 0) + 1
        usage["model_tokens"][key] = usage["model_tokens"].get(key, 0) + total
        usage["cost_usd"] += llmcost.cost_of(provider, model, p, c, cached, peak) or 0.0
    llmcost.record(provider, model, task, prompt_tokens=p, completion_tokens=c,
                   cached_tokens=cached, total_tokens=total,
                   latency_ms=latency_ms, failed=failed, peak=peak)


def _call(provider, prompt, task=None):
    model = _model_for(provider, task)
    is_reasoning = task in config.REASONING_TASKS
    # Reasoning models think longer before answering; give them more headroom.
    timeout = 150 if is_reasoning else 60
    started = time.time()
    if provider == "groq":
        if not config.GROQ_API_KEY:
            raise RuntimeError("no groq key")
        r = _post_chat(
            "https://api.groq.com/openai/v1/chat/completions",
            config.GROQ_API_KEY,
            {"model": model,
             "messages": [{"role": "user", "content": prompt}],
             "temperature": 0.4,
             "response_format": {"type": "json_object"}},
            timeout, "groq", model)
        data = r.json()
        _record("groq", model, task, _openai_usage(data),
                (time.time() - started) * 1000)
        return data["choices"][0]["message"]["content"]
    if provider in ("openai", "deepseek"):
        key = config.DEEPSEEK_API_KEY if provider == "deepseek" else config.OPENAI_API_KEY
        if not key:
            raise RuntimeError(f"no {provider} key")
        base = ("https://api.deepseek.com/v1" if provider == "deepseek"
                else "https://api.openai.com/v1")
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if provider == "deepseek" and is_reasoning:
            # DeepSeek "thinking mode": the model reasons step-by-step before its
            # final answer. It ignores temperature and may reject response_format,
            # so we omit both and pull JSON from the answer via _extract_json.
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = config.DEEPSEEK_REASONING_EFFORT
        else:
            body["temperature"] = 0.4
            body["response_format"] = {"type": "json_object"}
            if provider == "deepseek":
                body["thinking"] = {"type": "disabled"}  # keep base tasks fast/cheap
        r = _post_chat(f"{base}/chat/completions", key, body, timeout, provider, model)
        data = r.json()
        _record(provider, model, task, _openai_usage(data),
                (time.time() - started) * 1000)
        return data["choices"][0]["message"]["content"]
    if provider == "gemini":
        if not config.GEMINI_API_KEY:
            raise RuntimeError("no gemini key")
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent",
            headers={"x-goog-api-key": config.GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json",
                                       "temperature": 0.4}},
            timeout=timeout)
        if r.status_code == 429:
            raise RateLimited()
        r.raise_for_status()
        data = r.json()
        _record("gemini", model, task, _gemini_usage(data),
                (time.time() - started) * 1000)
        return data["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError(f"unknown provider {provider}")


def _extract_json(text):
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    return json.loads(m.group(0) if m else text)


# ---------------------------------------------------------------- mock mode
def _keywords(prompt, n=3):
    stop = set(("the a an of to in on for and or is are was were with from by at as "
                "it its this that news said says new will would could "
                # prompt-template words, so mock answers reflect content not instructions
                "reply only json claims claim headline narrative story stories items "
                "item source sources verified write watch hook happened matters words "
                "plain language catchy strictly true factual element appear below "
                "these related name explain sentences short trend reader profile "
                "recent last hours accelerating emerging signal state most important "
                "checkable list step plainly explained confidence chain trace real "
                "causal economic policy affects other unrelated surface skeptical "
                "meaningful link summary title extract answer question reader "
                "assistant accurate uncertainty admit words tailor followups "
                "natural follow profile intelligence "
                # finance prompt scaffolding — same reason as the block above:
                # without these the finance prompts' own instructions outweigh
                # the article text and mock mode "extracts" entities called
                # "Brief", "Crore" and "Event".
                "brief crore lakh entity entities metric metrics table tables "
                "column columns relationship relationships verbatim predicate "
                "subject object actor actors sentiment stage perspective "
                "scenario scenarios forecast probability horizon magnitude "
                "drivers precedent invalidation invalidates observable "
                "threshold financial advice recommend target price value unit "
                "currency period basis reported guidance consensus estimate "
                "revenue earnings none null organization regulator geography "
                "sector arc cascade macro second order hops confidence").split())
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", prompt)]
    freq = {}
    for w in words:
        if w not in stop:
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:n]]


def _mock(task, prompt):
    # Recorded like any other call — a mock run should still show up in the
    # spend report, at its true cost of zero, so "nothing was billed" and
    # "nothing was measured" can be told apart.
    _record("mock", "mock", task, (0, 0, 0, 0))
    kws = _keywords(prompt)
    label = " ".join(k.capitalize() for k in kws[:2]) or "General News"
    if task == "entities":
        return {"entities": kws, "sectors": kws[:1], "regions": []}
    if task == "entities_batch":
        # Answer PER ITEM from that item's own line, not from the whole prompt:
        # a batch mock that returns the same keywords for all 20 items would
        # exercise the parsing and hide the failure that actually matters
        # (answers landing on the wrong index).
        out = []
        for line in re.findall(r"^\[(\d+)\] (.*)$", prompt, re.M):
            idx, text = int(line[0]), line[1]
            k = _keywords(text)
            out.append({"i": idx, "entities": k, "sectors": k[:1], "regions": []})
        return {"items": out}
    if task == "trend":
        # Single-call shape: split the numbered items into 1-2 pseudo-trends so
        # mock demos exercise the plural path.
        idxs = [int(x) for x in re.findall(r"^\[(\d+)\]", prompt, re.M)]
        if len(idxs) < 2:
            return {"trends": []}
        mid = max(1, len(idxs) // 2)
        groups = [idxs[:mid], idxs[mid:]] if len(idxs) >= 4 else [idxs]
        names = [f"{label} gathers momentum", f"{label} ripple widens"]
        trends = [{"name": names[i % len(names)],
                   "narrative": f"Multiple outlets are converging on {label.lower()}, "
                                f"suggesting sustained attention in this area.",
                   "sectors": kws[:2], "regions": [], "members": g}
                  for i, g in enumerate(groups) if len(g) >= 2]
        micro = ([{"name": f"{label} stirs early",
                   "narrative": f"Early, thin coverage of {label.lower()} — worth watching.",
                   "members": idxs[:2]}] if len(idxs) >= 2 else [])
        return {"trends": trends, "micro_trends": micro}
    if task == "micro_trend":
        idxs = [int(x) for x in re.findall(r"^\[(\d+)\]", prompt, re.M)]
        if len(idxs) < 2:
            return {"micro_trends": []}
        return {"micro_trends": [{
            "name": f"{label} accelerating",
            "signal": f"Coverage of {label.lower()} is picking up over the last "
                      f"72 hours — worth watching.",
            "members": idxs[:3]}]}
    if task == "same_story":
        # Mock cannot judge whether two headlines are the same event; answering
        # "none" keeps borderline pairs unmerged, which is the safe default.
        return {"same": []}
    if task == "connection":
        return {"chain": f"Both stories touch {label.lower()}; a shift in one can move "
                         f"costs, policy or sentiment that feeds the other.",
                "confidence": 0.62, "affected": kws}
    if task == "claims":
        return {"claims": [f"The report about {label.lower()} is accurately attributed.",
                           "Key figures quoted match the original source."]}
    if task == "verify":
        return {"verdicts": [{"claim": "primary claim", "verdict": "corroborated",
                              "note": "mock mode: heuristic verdict"}],
                "score": 78,
                "note": "Mock estimate based on source count; add an LLM key for real verification."}
    if task == "story":
        return {"headline": f"{label}: The Shift No One Priced In",
                "what_happened": f"Reports centred on {label.lower()} emerged across "
                                 f"several sources today. The core development is "
                                 f"summarised in the linked articles, and momentum has "
                                 f"been building in this space over recent days. "
                                 f"(Placeholder text — this story was generated without "
                                 f"an LLM key or after hitting a rate limit.)",
                "why_it_matters": f"If it holds, this reshapes how {label.lower()} is "
                                  f"priced and who carries the cost. Watch for follow-up "
                                  f"coverage and official responses in the coming days. "
                                  f"(Placeholder text.)"}
    if task in ("signals", "signals_unit"):
        # Parse two story ids from digest lines so mock demos still work.
        ids = re.findall(r"^([0-9a-f]{12}) \|", prompt, re.M)[:3]
        if len(ids) < 2:
            return {"signals": []}
        return {"signals": [{
            "title": f"{label} ripple effect forming",
            "prediction": f"Pressure building around {label.lower()} may surface in "
                          f"adjacent sectors within weeks. (Placeholder — mock mode.)",
            "chain": "Story A raises costs/attention → intermediaries adjust → "
                     "second-order effect appears in a different sector.",
            "watch": "Follow-up coverage volume and supplier pricing announcements.",
            "affected": kws, "horizon": "2-8 weeks", "confidence": 0.45,
            "story_ids": ids}]}
    if task == "ask":
        return {"answer": f"Based on the covered reporting around {label.lower()}, the "
                          f"short answer is that the situation is still developing. The "
                          f"linked sources agree on the core facts; the open questions "
                          f"are timing and second-order effects. Add a free LLM key to "
                          f".env for real answers.",
                "followups": ["What happens next?", "How does this affect my area?",
                              "Show opposing viewpoints"]}
    if task == "framing":
        # Mock cannot read emphasis, so it places every outlet it can see at 0
        # ("framed it the same way") rather than inventing a disagreement. That
        # is also the honest answer whenever the real model is unsure, so the
        # mock exercises the same client path a cautious real answer produces.
        srcs = re.findall(r"^\[([^\]]+)\]$", prompt, re.M)
        return {"axis": {"left": f"{label} as cause", "right": f"{label} as effect"},
                "positions": [{"source": s, "pos": 0.0,
                               "note": "mock mode: framing not assessed"}
                              for s in srcs],
                "missing": ""}
    if task == "personalize":
        return {"impact_text": f"Given your profile, developments in {label.lower()} "
                               f"could affect your sector or region. Worth a closer read.",
                "impact_score": 1}
    # ------------------------------------------------------ finance pipeline
    # Mock mode has to exercise the finance path end to end (it is how the
    # tests run, and how the pipeline behaves locally with no keys), so these
    # answer in the real shapes. Every figure is anchored to a number actually
    # present in the prompt rather than invented — the finance schemas reject a
    # metric whose verbatim span carries no digit, and mock output that could
    # not survive its own validator would be a useless rehearsal.
    if task == "fin_extract":
        # NO entities and NO relationships, deliberately. Mock mode cannot read
        # which words in a brief are companies, and its keyword guesses ("Event",
        # "Stake", "Crore") would be written into the knowledge graph as nodes —
        # leaving a graph that looks populated while every walk over it is
        # meaningless, which is worse than an empty one. Same reasoning as the
        # same_story and framing mocks above: when the mock cannot judge, it
        # declines rather than inventing. The metric row IS emitted, anchored to
        # a number actually present in the brief, so the extraction path and its
        # verbatim rule are still exercised end to end.
        num = re.search(r"\d[\d,]*(?:\.\d+)?", prompt)
        metrics = []
        if num:
            metrics = [{"name": "figure", "value": float(num.group(0).replace(",", "")),
                        "unit": "none", "currency": "none", "period": "",
                        "basis": "unknown", "entity": "", "yoy_change": None,
                        "verbatim": f"figure reported as {num.group(0)} "
                                    f"(mock mode: not a real extraction)"}]
        return {"event_type": "other", "entities": [],
                "metrics": metrics, "relationships": []}
    if task == "fin_sentiment":
        m = re.search(r"^Actors named or clearly implied: (.+)$", prompt, re.M)
        actors = [a.strip() for a in (m.group(1) if m else "").split(",") if a.strip()]
        return {"actors": [{"actor": a, "role": "executive",
                            "knows": "mock mode: perspective not simulated",
                            "wants": "mock mode: incentive not simulated",
                            "position": "", "sentiment": 0.0, "confidence": 0.2}
                           for a in actors[:6]],
                "rationale": "Mock mode: actor perspectives were not simulated."}
    if task == "fin_story":
        return {"headline": f"{label}: What The Numbers Show",
                "what_happened": f"Reporting centred on {label.lower()} emerged across "
                                 f"several outlets. The figures and relationships in the "
                                 f"merged brief are carried through unchanged. "
                                 f"(Placeholder text — generated without an LLM key.)",
                "why_it_matters": f"If it holds, this changes the economics around "
                                  f"{label.lower()} and who carries the cost. "
                                  f"(Placeholder text.)",
                "economic_drivers": kws[:2]}
    if task == "fin_trend":
        ids = re.findall(r"^([0-9a-f]{12}) \|", prompt, re.M)[:3]
        if len(ids) < 2:
            return {"trends": []}
        return {"trends": [{
            "name": f"{label} pressure building",
            "narrative": f"Costs around {label.lower()} are moving in one direction "
                         f"across several companies. (Placeholder — mock mode.)",
            "arc": [f"{label.lower()} costs rise", "margins compress",
                    "spending slows"],
            "sectors": kws[:2], "tickers": [], "macro_factors": kws[:1],
            "second_order": [], "confidence": 0.4, "story_ids": ids}]}
    if task == "fin_causal":
        # The chain's STRUCTURE is built from the graph before this call, so the
        # mock only has to supply words. One rewritten step per step it was
        # given, in order, because the caller matches them positionally.
        given = re.findall(r"^\s*(\d+)\.\s+(.+?)(?:\s{2}\(|$)", prompt, re.M)
        return {"title": f"How {label} pressure travels",
                "catalyst_event": f"Something changes at {label}. (Placeholder — mock mode.)",
                "transmission_channel": "The effect passes along recorded relationships.",
                "terminal_outcome": "A reader eventually notices it in prices. (Placeholder.)",
                "catalyst_domain": "Placeholder Domain",
                "time_horizon": "3-6 months",
                "steps": [{"stage_name": "Trigger" if i == 0 else "Ripple",
                           "action_or_friction": f"{name.strip()} passes the effect on. "
                                                 f"(Placeholder — mock mode.)"}
                          for i, (_, name) in enumerate(given)],
                "historical_precedent": "",
                "dampeners": []}
    if task == "fin_forecast":
        return {"title": f"{label} pressure may spread to adjacent sectors",
                "scenarios": [
                    {"kind": "base", "probability": 0.5,
                     "thesis": "Conditions hold broadly as reported, with the effect "
                               "contained to the companies already named. "
                               "(Placeholder — mock mode.)",
                     "horizon": "short_term", "direction": "flat",
                     "magnitude": "modest", "drivers": kws[:2], "precedent": "",
                     "affected_sectors": kws[:1], "affected_tickers": [],
                     "confidence": 0.4},
                    {"kind": "bull", "probability": 0.25,
                     "thesis": "The pressure eases sooner than the reporting implies "
                               "and the effect does not travel. (Placeholder.)",
                     "horizon": "long_term", "direction": "up",
                     "magnitude": "modest", "drivers": kws[:1], "precedent": "",
                     "affected_sectors": kws[:1], "affected_tickers": [],
                     "confidence": 0.3},
                    {"kind": "bear", "probability": 0.25,
                     "thesis": "The pressure transmits through suppliers and lenders "
                               "to a wider set of companies. (Placeholder.)",
                     "horizon": "long_term", "direction": "down",
                     "magnitude": "material", "drivers": kws[:2], "precedent": "",
                     "affected_sectors": kws[:1], "affected_tickers": [],
                     "confidence": 0.3}],
                "short_term": "Little visible change in the first weeks. (Placeholder.)",
                "long_term": "Direction depends on whether the pressure persists. "
                             "(Placeholder.)",
                "risks": ["Mock mode: no real analysis was performed."],
                "dependencies": ["An LLM key being configured."],
                "invalidation": [{"observable": "the next scheduled results or policy "
                                                "statement from the named parties",
                                  "threshold": "any reversal of the reported direction",
                                  "source": "company filings", "invalidates": ["bear"]}],
                "confidence": 0.35}
    return {}
