"""Configuration. Reads .env if present, else environment, else safe defaults."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _unquote(v):
    """Strip one matching pair of surrounding quotes.

    Every other dotenv reader does this, so a value is routinely pasted into
    .env already quoted — and a JSON value has to be quoted to survive a shell.
    Without it the quotes became part of the string: LLM_PRICES arrived as
    `'{"model": [...]}'`, json.loads rejected it, the whole rate card was
    discarded, and every model silently reported $0.00 spend. Nothing failed
    loudly, because a missing rate is a legitimate state."""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _load_dotenv():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), _unquote(v.strip()))

_load_dotenv()

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "mock").lower()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
# DeepSeek does reasoning via a thinking-mode toggle on its models (the legacy
# 'deepseek-reasoner' name retires 2026-07-24). deepseek-v4-pro is the powerful
# tier; effort high|max controls how long it reasons before answering.
DEEPSEEK_REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "high")

# --- DeepSeek peak-valley pricing (from mid-July 2026) ---
# DeepSeek charges 2x on ALL billing items during declared peak hours. To avoid
# overpaying, during those windows we demote DeepSeek to the back of the `auto`
# provider order, so the free providers (Groq/Gemini) run first and DeepSeek is
# only a last resort. Off-peak it stays first (best reasoning). Set
# DEEPSEEK_AVOID_PEAK=0 to always keep DeepSeek's normal priority regardless.
DEEPSEEK_AVOID_PEAK = os.environ.get(
    "DEEPSEEK_AVOID_PEAK", "1").lower() not in ("0", "false", "no")

def _parse_hour_windows(s):
    """Parse "1-4,6-10" into [(1, 4), (6, 10)] — half-open [start, end) UTC hours."""
    out = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                out.append((int(a), int(b)))
            except ValueError:
                pass
    return out

# DeepSeek's declared peak hours in UTC: 01:00–04:00 and 06:00–10:00.
DEEPSEEK_PEAK_WINDOWS_UTC = _parse_hour_windows(
    os.environ.get("DEEPSEEK_PEAK_WINDOWS_UTC", "1-4,6-10"))

# Tasks that need deep reasoning use each provider's stronger model when set.
# Empty string = that provider uses its base model for reasoning tasks too.
#
# The finance tasks are here because it was measured, not assumed. Given one
# story carrying two separate penalties (Rs 75 crore on one bank, Rs 63 crore on
# another), llama-3.3-70b merged them into "a range of Rs 63 to Rs 75 crore" —
# a figure no outlet reported — and did so even after the prompt was made
# explicit that two different facts are not a discrepancy. The same brief on a
# stronger model kept the two fines attached to their own companies. Numerical
# attribution is the whole point of this pipeline, so it gets the better model.
# fin_sentiment stays on the base model: it is qualitative, and it is the one
# finance call whose failure is not fatal to the story.
REASONING_TASKS = set(t.strip() for t in os.environ.get(
    "REASONING_TASKS",
    "signals,signals_unit,trend,fin_extract,fin_story,fin_trend,fin_forecast"
).split(","))
# Under LLM_PROVIDER=auto, reasoning tasks (trend/forecast) try these providers
# in order — strongest thinking model first — falling through on missing key or
# rate-limit. Ordinary tasks keep the cheaper free-first order below.
# Gemini FIRST for reasoning, and the ordering here is about money, not about
# the rate card.
#
# `trend` and `signals` are the most expensive unit of work in the pipeline
# (~8,000 tokens a call), so whoever serves them dominates the bill. The previous
# order put DeepSeek first — a PAID provider — so every one of those calls was
# billed before a free provider was even offered it.
#
# gemini-3.5-flash has the most alarming rate card of any model here
# ($1.50/$7.50 per 1M, ~7x deepseek-v4-pro for the same call) and is nonetheless
# the right first choice, because it is inside the Gemini FREE tier: that price
# is what the traffic WOULD cost on a paid plan, not what it costs. It is also a
# genuine thinking model, so free and strong are not in tension here. Groq
# follows (also free, but its reasoning model is the plain llama-3.3-70b).
# DeepSeek and OpenAI are the paid overflow once the free quotas are spent, in
# that order — deepseek-v4-pro reasons harder, which is the reason REASONING_TASKS
# exists at all; swap them if cost matters more than depth at the margin
# (gpt-5.6-luna is ~2.3x cheaper per trend call).
#
# See FREE_PROVIDERS: a provider being free is a fact about the account, not
# something derivable from the rate card, so it has to be stated.
REASONING_PROVIDER_ORDER = [p.strip() for p in os.environ.get(
    "REASONING_PROVIDER_ORDER", "gemini,groq,deepseek,openai").split(",") if p.strip()]
GROQ_REASONING_MODEL = os.environ.get("GROQ_REASONING_MODEL", "")
GEMINI_REASONING_MODEL = os.environ.get("GEMINI_REASONING_MODEL", "gemini-2.5-flash")
DEEPSEEK_REASONING_MODEL = os.environ.get("DEEPSEEK_REASONING_MODEL", "deepseek-v4-pro")
OPENAI_REASONING_MODEL = os.environ.get("OPENAI_REASONING_MODEL", "")

# --- Task tiers -------------------------------------------------------------
# REASONING_TASKS above answers "does this task need the strong model", which is
# only half the routing question. The other half — "is this task trivial enough
# that it must NEVER reach an expensive model" — had no answer at all, so every
# non-reasoning task shared one order and one model per provider. That is how
# `entities` (a title+summary -> list-of-names extraction) ended up being served
# by whatever the OPENAI_MODEL of the day was: it is not a reasoning task, so it
# fell through to the ordinary path, and the ordinary path has no ceiling.
#
# Three tiers now, each with its own provider order AND its own model per
# provider:
#   cheap     — mechanical extraction/classification. Pinned to small models.
#   standard  — the previous default behaviour, unchanged.
#   reasoning — REASONING_TASKS, unchanged.
# A task not named in CHEAP_TASKS or REASONING_TASKS is "standard", so adding a
# new task keeps today's behaviour until it is deliberately classified.
CHEAP_TASKS = set(t.strip() for t in os.environ.get(
    "CHEAP_TASKS",
    "entities,entities_batch,same_story,claims,framing,personalize"
).split(",") if t.strip())

def tier_of(task):
    """'cheap' | 'standard' | 'reasoning' for one task name."""
    if task in REASONING_TASKS:
        return "reasoning"
    if task in CHEAP_TASKS:
        return "cheap"
    return "standard"

# Provider order per tier under LLM_PROVIDER=auto. Standard keeps the historical
# free-first order verbatim. Cheap uses the same order but reaches each provider's
# small model (below) — the paid provider staying last is NOT by itself enough of
# a guard, because "last" is exactly where an exhausted free tier sends everything.
STANDARD_PROVIDER_ORDER = [p.strip() for p in os.environ.get(
    "STANDARD_PROVIDER_ORDER", "groq,gemini,deepseek,openai").split(",") if p.strip()]
CHEAP_PROVIDER_ORDER = [p.strip() for p in os.environ.get(
    "CHEAP_PROVIDER_ORDER", "groq,gemini,deepseek,openai").split(",") if p.strip()]

# The small model each provider serves cheap-tier tasks with. Defaults to that
# provider's base model, EXCEPT OpenAI: its base model is whatever frontier model
# the operator has configured for storytelling, and pointing entity extraction at
# it is the single largest avoidable line on the bill.
GROQ_CHEAP_MODEL = os.environ.get("GROQ_CHEAP_MODEL", "") or GROQ_MODEL
GEMINI_CHEAP_MODEL = os.environ.get("GEMINI_CHEAP_MODEL", "") or GEMINI_MODEL
DEEPSEEK_CHEAP_MODEL = os.environ.get("DEEPSEEK_CHEAP_MODEL", "") or DEEPSEEK_MODEL
OPENAI_CHEAP_MODEL = os.environ.get("OPENAI_CHEAP_MODEL", "gpt-4o-mini")

# --- Spend ceilings ---------------------------------------------------------
# Dollars of LLM spend allowed per UTC day across every provider. Above it, any
# provider whose rate card is non-zero is dropped from the running order and the
# call returns None — the failure path every caller already handles by skipping
# the item and retrying next run. 0 = no ceiling (previous behaviour).
#
# A ceiling rather than a throttle on purpose: LLM_MAX_CALLS_PER_MIN bounds the
# RATE, which bounds nothing about the bill when the per-call price changes
# underneath it. This bounds the bill directly.
LLM_DAILY_BUDGET_USD = float(os.environ.get("LLM_DAILY_BUDGET_USD", "0") or 0)

# Providers whose calls are genuinely free up to a daily quota. Their rate card
# entry is still meaningful — it answers "what would this have cost on the paid
# tier", which is exactly the number needed to decide whether to upgrade — but it
# is NOT money, so it must not count against the daily budget and must not get a
# free provider dropped for being "expensive". Without this the ceiling would
# count Groq at $0.59/1M it never charged and start refusing the paid providers
# that were doing the real work.
#
# The real constraint on a free provider is its QUOTA, not its price, so these
# are governed by PROVIDER_DAILY_CALL_LIMITS instead. Set this to empty if you
# move Groq/Gemini onto paid plans.
FREE_PROVIDERS = set(p.strip().lower() for p in os.environ.get(
    "FREE_PROVIDERS", "groq,gemini").split(",") if p.strip())

def _parse_int_map(raw):
    """Parse '{"groq": 14400, "gemini": 1000}' into {str: int}, skipping junk."""
    raw = _unquote(raw.strip())   # see _unquote: quoted JSON must not be dropped
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    out = {}
    for k, v in (data or {}).items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(k).strip().lower()] = n
    return out

# Per-provider requests-per-DAY ceilings, e.g. {"groq": 14400, "gemini": 1000}.
# Free tiers are metered per day as well as per minute, and only the per-minute
# half was modelled — so a provider that had spent its daily allowance was
# re-probed every 15 minutes forever while the paid provider carried the work.
# Empty = no daily ceiling is enforced for that provider (the escalating bench in
# llm.py still limits the damage; this just avoids paying to discover it).
PROVIDER_DAILY_CALL_LIMITS = _parse_int_map(
    os.environ.get("PROVIDER_DAILY_CALL_LIMITS", ""))

# Repeated 429s from one provider double its bench each time (15m -> 30m -> 1h
# ...), capped here, and reset by the next success. Without escalation a provider
# whose DAILY quota is gone gets re-probed four times an hour until midnight.
PROVIDER_BENCH_MAX_SECONDS = int(os.environ.get("PROVIDER_BENCH_MAX_SECONDS",
                                                str(6 * 3600)))

# Identical prompts are answered from SQLite instead of the provider for this
# many seconds. The pipeline is re-runnable by design and each stage skips work
# already done, but a stage re-run after a partial failure re-asks the prompts
# that DID succeed. 0 disables the cache.
LLM_CACHE_TTL_SECONDS = int(os.environ.get("LLM_CACHE_TTL_SECONDS", str(24 * 3600)))

NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "")

# --- Live dynamic-hero section (scores / breaking / finance / events) ---
# Free sports API for live scores. Blank key = no score cards (breaking/finance
# still work). Provider is currently "thesportsdb" (free tier; key "3" is their
# public test key). Sports to follow: soccer, cricket.
SPORTS_API_KEY = os.environ.get("SPORTS_API_KEY", "")
SPORTS_PROVIDER = os.environ.get("SPORTS_PROVIDER", "thesportsdb").lower()
# Minutes between fast live refreshes (breaking sweep + sports/finance). This is
# the separate, lightweight job — NOT the 3h story pipeline.
LIVE_REFRESH_MINUTES = float(os.environ.get("LIVE_REFRESH_MINUTES", "5"))
# Finance cards (news + optional index snapshot via keyless Stooq). Set to 0/false
# to disable the finance category server-side.
FINANCE_ENABLED = os.environ.get("FINANCE_ENABLED", "1").lower() not in ("0", "false", "no")
# A story is "breaking" when corroborated by >= this many distinct sources within
# BREAKING_WINDOW_HOURS, or when its text hits a high-signal keyword.
BREAKING_MIN_SOURCES = int(os.environ.get("BREAKING_MIN_SOURCES", "3"))
BREAKING_WINDOW_HOURS = float(os.environ.get("BREAKING_WINDOW_HOURS", "6"))

# Required for /admin/* endpoints (pipeline runs, intel rebuild, usage). With no
# token set, admin endpoints refuse — the API is public, so they must never be open.
# Stripped: a value pasted into a hosting dashboard routinely arrives with a
# trailing newline, and the resulting mismatch is indistinguishable from a wrong
# password.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
#: Below this many characters an admin token is guessable at internet scale.
#: Warned about at startup rather than enforced — refusing to boot over it would
#: take the whole API down for a problem that only affects /admin/*.
ADMIN_TOKEN_MIN_LEN = 24
# Comma-separated CORS origin allowlist for browsers; * = any (beta default).
ALLOWED_ORIGINS = [o.strip() for o in
                   os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Public URL of the static web SPA — where the crawler-facing OG routes (/s /t /g)
# redirect human visitors, and the base for sitemap links.
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "https://descry.onrender.com").rstrip("/")
# 1200x630 preview image for social/OG cards. Replace the placeholder with a real
# hosted PNG. Blank = omit og:image.
OG_IMAGE_URL = os.environ.get("OG_IMAGE_URL", f"{WEB_BASE_URL}/og.png")
PIPELINE_INTERVAL_HOURS = float(os.environ.get("PIPELINE_INTERVAL_HOURS", "6"))
# Feed/trend/forecast ordering: how sharply older items are discounted relative
# to fresher ones of the same impact, in units of PIPELINE_INTERVAL_HOURS-sized
# "cycles" (see app/ranking.py). Higher = recency matters more; an item several
# cycles old is discounted toward zero regardless of impact at any value > 1.
RANK_GRAVITY = float(os.environ.get("RANK_GRAVITY", "1.5"))
# Illustrated stories rank slightly above comparable text-only ones — a feed of
# picture cards with holes in it reads as broken. Deliberately a small MULTIPLIER
# on the final score rather than a sort key of its own, so the existing
# recency/impact hierarchy still decides the order and artwork only settles
# near-ties. It must stay well below the cost of being one pipeline cycle older
# ((1+1)**RANK_GRAVITY = 2.83x at the defaults) or a stale-but-illustrated story
# could climb back over fresh reporting, which is the exact failure ranking.py
# exists to prevent. See test: an image must never beat a fresher story.
RANK_IMAGE_BOOST = float(os.environ.get("RANK_IMAGE_BOOST", "1.15"))
# How much a story in one of the reader's chosen topics outranks one that is
# not. The default feed used to ignore interests completely, which is why a
# reader who never picked Sports still got a feed that was half sports: Sports
# is the largest topic by ingest volume (23.5% of the catalogue), its feeds
# publish constantly, and single-source stories all share the same impact — so
# recency alone decided the order and the loudest-publishing topic won.
# 8x is worth roughly three pipeline cycles of ageing at the default gravity:
# a chosen topic leads while it is anywhere near as fresh, and still loses to
# genuinely newer reporting once it is a day old.
RANK_INTEREST_BOOST = float(os.environ.get("RANK_INTEREST_BOOST", "8.0"))
# Corroboration's weight in the feed order, applied as
# RANK_CRED_FLOOR + credibility/100 * RANK_CRED_WEIGHT.
# The feed ranked on ARTICLE COUNT alone and ignored `credibility` — the figure
# the reader is actually shown ("Nearly all sources agree · 85") — so a
# single-source story scored 10 tied exactly with a single-source story scored
# 98. At the defaults the spread is 0.6x .. 1.5x, about one cycle of ageing:
# enough to separate well-corroborated reporting from thin reporting, not
# enough to park a well-corroborated old story above fresh news.
RANK_CRED_FLOOR = float(os.environ.get("RANK_CRED_FLOOR", "0.6"))
RANK_CRED_WEIGHT = float(os.environ.get("RANK_CRED_WEIGHT", "0.9"))
# Ingest freshness: skip feed entries published more than this many hours ago.
# Feeds carry a long tail (some hold 200 items), so without a gate every run
# re-considers week-old news. Keep it comfortably WIDER than the pipeline
# interval so nothing falls through a gap when a run is late or skipped.
# 0 disables the gate. Entries with no date are always kept (age unknown).
INGEST_MAX_AGE_HOURS = float(os.environ.get("INGEST_MAX_AGE_HOURS", "12"))
# Max stories built per run — keeps a single run inside LLM budgets.
MAX_STORIES_PER_RUN = int(os.environ.get("MAX_STORIES_PER_RUN", "20"))
# How far back Storyteller looks for a story to continue (match by event_id,
# or legacy article-overlap). 7 days, matching every other retention constant
# here (the feed itself, Foresight.WINDOW_DAYS, and the orphan-article query
# just below) rather than some wider "just in case" margin — a story can only
# ever receive a new article via a still-live trend (retired within a single
# run once TrendLinker stops re-proposing it, so it can't sit dormant for
# long while staying "live") or an orphan article (already hard-bounded to
# fetched_at > now-7d), so nothing that could legitimately continue a story
# falls outside this window. Bounding here avoids reloading every story ever
# created (and parsing its article_ids/trend_ids JSON) on every single run —
# a cost with no ceiling that grows as the story table grows, and the prime
# suspect for a multi-minute memory climb observed during pipeline runs on
# the 512MB instance (the per-event split means far more story rows
# accumulate now than before it shipped).
STORYTELLER_HISTORY_DAYS = float(os.environ.get("STORYTELLER_HISTORY_DAYS", "7"))

# Global cap on REAL LLM calls per rolling minute, across every task and provider.
# Protects free-tier RPM limits (Groq is 30/min) and bounds token burn. 0 = off.
LLM_MAX_CALLS_PER_MIN = int(os.environ.get("LLM_MAX_CALLS_PER_MIN", "30"))

# Per-provider slice of that budget, {provider: calls-per-rolling-minute}.
# The global cap alone cannot spread load: the first provider in the running
# order is offered every call, so with a 30/min global cap and Groq's own 30/min
# limit, Groq alone absorbed the whole budget and sat permanently at its ceiling
# — 429, benched, and the rest of the run drained to whoever was last in the
# order. Giving each provider a slice below its own limit means a provider that
# is momentarily full is SKIPPED (the next one in the order takes the call)
# rather than pushed until it refuses. 0 / absent = no per-provider cap.
#
# Gemini is listed because _pace BLOCKS (it sleeps out the per-model interval)
# while this SKIPS to the next provider. With Gemini now first for reasoning and
# paced at 6.7s for gemini-3.5-flash, a burst of trend calls would otherwise
# serialise behind those sleeps instead of spilling onto the other free provider.
PROVIDER_MAX_CALLS_PER_MIN = _parse_int_map(
    os.environ.get("PROVIDER_MAX_CALLS_PER_MIN", '{"groq": 25, "gemini": 14}'))

# ------------------------------------------------- LLM spend accounting
# The rate card, in US dollars per 1,000,000 tokens, as
# [input, output, cached_input], keyed by "provider/model".
#
# Seeded ONLY with list prices that are actually known. A model with no entry
# here is reported as UNPRICED — its calls and tokens still appear in every
# report and the admin page names it — rather than being costed at zero. That
# distinction matters: zero is a claim that the model is free, and a wrong zero
# frozen into a month of history is worse than a visible gap, because the token
# counts are stored and a gap can be repriced (POST /admin/llm-reprice) while a
# confident wrong number never gets questioned.
#
# Provider price pages change without notice, so treat these as a starting
# point and keep the real rates in LLM_PRICES. Confirm against the provider's
# invoice before reporting a figure to anyone.
LLM_PRICE_DEFAULTS = {
    "mock/mock": [0.0, 0.0, 0.0],          # no network call, definitionally free
    "groq/llama-3.3-70b-versatile": [0.59, 0.79, 0.59],
    "gemini/gemini-2.5-flash-lite": [0.10, 0.40, 0.025],
    "gemini/gemini-2.5-flash": [0.30, 2.50, 0.075],
    "openai/gpt-4o-mini": [0.15, 0.60, 0.075],
}

#: Problems found while parsing the rate card, reported rather than raised —
#: a bad price must not stop the API booting, but it must not be invisible either.
_price_errors = []


def _parse_prices(raw):
    """Parse the LLM_PRICES override: a JSON object of
    {"provider/model": [input, output, cached_input]} in $ per 1M tokens.
    cached_input is optional and defaults to the input rate. A malformed entry
    is skipped rather than taken as zero — see the note above about wrong
    numbers being worse than missing ones."""
    # Unquoted here as well as in _load_dotenv, because a hosting dashboard sets
    # the variable DIRECTLY in the environment — _load_dotenv never runs for it,
    # so a value pasted with the quotes it needed in a shell would be dropped
    # exactly the way the .env one was.
    raw = _unquote(raw.strip())
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        # Returning {} is still right — a wrong price frozen into a month of
        # history is worse than a visible gap — but doing it in SILENCE is how an
        # entire configured rate card went missing while the cost report kept
        # printing $0.00 as though that were a measurement. So it is also
        # recorded, and surfaced at startup and on /admin/usage.
        _price_errors.append(f"LLM_PRICES is not valid JSON ({e}); the whole "
                             f"rate card was ignored and every model reads as "
                             f"unpriced")
        return {}
    out = {}
    for key, val in (data or {}).items():
        try:
            nums = [float(x) for x in (val if isinstance(val, list) else [val])]
        except (TypeError, ValueError):
            continue
        if not nums or any(n < 0 for n in nums):
            continue
        while len(nums) < 3:
            nums.append(nums[0] if len(nums) == 1 else nums[-1])
        out[str(key).strip().lower()] = nums[:3]
    return out

#: Rates supplied by the operator. Kept separate from the defaults so the admin
#: rate card can say which of the two a given number came from.
LLM_PRICES_ENV = _parse_prices(os.environ.get("LLM_PRICES", ""))

# Aggregated spend rows are tiny (bounded by distinct model x task combinations,
# not by call volume), and cost history is the whole point of keeping them, so
# the default is to keep them forever. Set a day count to prune anyway.
LLM_USAGE_RETAIN_DAYS = int(os.environ.get("LLM_USAGE_RETAIN_DAYS", "0"))

# --------------------------------------------------- entity extraction
# How many articles share one `entities_batch` call. The prompt's fixed
# instruction block is ~200 tokens wrapping ~100 tokens of article, so at one
# article per call roughly 70% of every entity call was re-sent boilerplate,
# ~1,200 times. Batching amortises it. Kept well below the point where a model
# starts dropping items from a long indexed list — a batch that silently returns
# 14 of 20 answers costs more than it saves, since the missing 6 are retried.
ENTITIES_BATCH_SIZE = int(os.environ.get("ENTITIES_BATCH_SIZE", "20"))
# Terms held in memory by the gazetteer index, most-seen first. A hard cap
# because this table grows with the news and every unbounded read on this box has
# eventually OOM-killed it. 0 disables gazetteer matching entirely (every article
# then takes the batched LLM path, which is still ~20x fewer calls than before).
GAZETTEER_MAX_TERMS = int(os.environ.get("GAZETTEER_MAX_TERMS", "20000"))
# Capitalised names an article may contain that the gazetteer does NOT recognise
# and still be answered from memory. 0 = the vocabulary must account for the
# whole article. Raising this trades entity accuracy for hit rate: 1 unknown name
# is often a person attached to a known organisation, but it is also exactly how
# a genuinely new company gets silently dropped from the graph. Change it only
# with the shadow-mode disagreement log in front of you.
GAZETTEER_MAX_UNKNOWN = int(os.environ.get("GAZETTEER_MAX_UNKNOWN", "0"))
# Terms seen exactly once and not since this many days are dropped.
GAZETTEER_RETAIN_DAYS = float(os.environ.get("GAZETTEER_RETAIN_DAYS", "60"))
# Log what the gazetteer WOULD have answered without using it, so its hit rate
# and its disagreements with the LLM can be measured before it is trusted. Set to
# 0 to let it actually short-circuit calls.
GAZETTEER_SHADOW = os.environ.get("GAZETTEER_SHADOW", "1").lower() not in ("0", "false", "no")

# Near-duplicate article merging (same story from different sources). Articles
# whose titles cosine-match at/above this are grouped so the LLM stages process
# the event ONCE (with all sources annotated) instead of once per source.
DEDUPE_SIMILARITY = float(os.environ.get("DEDUPE_SIMILARITY", "0.62"))
DEDUPE_WINDOW_DAYS = float(os.environ.get("DEDUPE_WINDOW_DAYS", "7"))

# --------------------------------------------------------- trend synthesis
# TrendLinker re-sent a 7-day article window on every run: ~60 items per topic,
# 11 topics, every PIPELINE_INTERVAL_HOURS. Consecutive runs overlapped ~96%, so
# the reasoning model re-derived the same trends from nearly the same corpus four
# times a day at ~8,000 tokens a call, and _reconcile_trends then matched the
# answers back to the trends they had been derived from the run before.
#
# Incremental runs instead show the model what it already concluded plus only the
# articles that could NOT be attached to an existing trend by similarity. Hours
# between full "see everything at once" passes — that pass is what lets a force
# spanning items the model was never shown together be spotted at all, so it is
# reduced in frequency, never removed. 0 = always full (previous behaviour).
TRENDS_FULL_PASS_HOURS = float(os.environ.get("TRENDS_FULL_PASS_HOURS", "24"))
# An article joins an existing trend without an LLM call when it clears BOTH:
# text similarity to the trend's name+narrative, and at least this many shared
# entity terms with the trend's current members. Two gates because either alone
# is wrong — text similarity puts any two stories about the same industry in one
# trend, and a shared entity is routine for a beat that covers one company.
TRENDS_ATTACH_COS = float(os.environ.get("TRENDS_ATTACH_COS", "0.30"))
TRENDS_ATTACH_MIN_ENTITIES = int(os.environ.get("TRENDS_ATTACH_MIN_ENTITIES", "2"))
# Below this many unattached articles a topic is skipped entirely on an
# incremental run. Asking a reasoning model to find a trend in two leftover
# articles produces a trend-shaped answer, not a trend.
TRENDS_MIN_NEW = int(os.environ.get("TRENDS_MIN_NEW", "3"))
# How far back an incremental run looks for unattached articles. Narrower than
# the full pass's 7 days so an article that never joins anything stops being
# re-sent every run forever.
TRENDS_INCREMENTAL_WINDOW_HOURS = float(
    os.environ.get("TRENDS_INCREMENTAL_WINDOW_HOURS", "48"))

# A trend the newest run no longer sees is RETIRED (soft-deleted), not dropped:
# it leaves the radar but its page and any shared link keep resolving, and it can
# come back with the same id if the story resurfaces. This is how long a retired
# trend stays readable before it is finally deleted.
TREND_RETIRE_PURGE_DAYS = float(os.environ.get("TREND_RETIRE_PURGE_DAYS", "30"))
# Analytics retention: raw per-visit rows are deleted after this many days. The
# pre-aggregated daily traffic counts are small and are kept.
ANALYTICS_RETAIN_DAYS = float(os.environ.get("ANALYTICS_RETAIN_DAYS", "180"))

# --- Grouping v2: TF-IDF + proper-noun anchors (see app/textmerge.py) ---
# Combined score = 0.55*tfidf_cos + 0.35*anchor_overlap + 0.10*time_proximity.
# A pair may only merge when BOTH floors are cleared AND it is inside the time
# window — the guards that let "Nintendo stops selling Switch" match a differently
# worded report of the same event while rejecting an unrelated "SwitchBot" story.
# Some genuine lexical relatedness is required even when anchors match: two
# stories can share "Meta"/"AI" and still be unrelated events (cos ~0.09).
DEDUPE_COS_FLOOR = float(os.environ.get("DEDUPE_COS_FLOOR", "0.10"))
DEDUPE_ANCHOR_FLOOR = float(os.environ.get("DEDUPE_ANCHOR_FLOOR", "0.50"))
DEDUPE_HIGH = float(os.environ.get("DEDUPE_HIGH", "0.52"))   # >= this: merge outright
DEDUPE_LOW = float(os.environ.get("DEDUPE_LOW", "0.34"))     # [LOW,HIGH): borderline
DEDUPE_WINDOW_HOURS = float(os.environ.get("DEDUPE_WINDOW_HOURS", "48"))
# A cluster member whose mean similarity to the rest falls below this is split
# back out — the anti-chaining guard (A~B, B~C must not silently merge A and C).
DEDUPE_COHESION = float(os.environ.get("DEDUPE_COHESION", "0.34"))

# A story is only split when its articles form this many or more distinct
# subjects. Size alone is never the trigger: a heavily-corroborated single event
# with 200 sources is one story, and a 2-article story about two unrelated things
# is two — what matters is whether the CONTENT is coherent, not how big it is.
STORY_SPLIT_MIN_ARTICLES = int(os.environ.get("STORY_SPLIT_MIN_ARTICLES", "2"))
# Linking thresholds for textmerge.subject_clusters. Two articles in one story
# are treated as the SAME subject when they share an anchor (a name/number) and
# have at least ANCHOR_LINK_COS text overlap, or when text alone reaches
# LINK_COS. Measured on real articles: same-subject pairs share 2-3 anchors at
# cosine 0.24-0.40, unrelated pairs share none at cosine 0.000 — so these sit in
# a wide empty gap, and both err toward keeping a story together.
STORY_SPLIT_ANCHOR_LINK_COS = float(os.environ.get("STORY_SPLIT_ANCHOR_LINK_COS", "0.05"))
STORY_SPLIT_LINK_COS = float(os.environ.get("STORY_SPLIT_LINK_COS", "0.45"))

# Borderline pairs resolved by ONE batched LLM call ("same event?"). Off = those
# pairs simply stay unmerged (fully deterministic, zero added cost).
SAMESTORY_VERIFY = os.environ.get("SAMESTORY_VERIFY", "1").lower() not in ("0", "false", "no")
SAMESTORY_MAX_PAIRS = int(os.environ.get("SAMESTORY_MAX_PAIRS", "15"))

# Fuller article text for grouped stories: fetched transiently for the prompt and
# NOT stored (ARCHITECTURE.md keeps only headline/summary/link). robots.txt is
# honoured. Disable to fall back to RSS summaries everywhere.
FULLTEXT_ENABLED = os.environ.get("FULLTEXT_ENABLED", "1").lower() not in ("0", "false", "no")
FULLTEXT_TIMEOUT = float(os.environ.get("FULLTEXT_TIMEOUT", "8"))
FULLTEXT_MAX_CHARS = int(os.environ.get("FULLTEXT_MAX_CHARS", "6000"))
FULLTEXT_MAX_PER_STORY = int(os.environ.get("FULLTEXT_MAX_PER_STORY", "4"))
# Hard ceiling on bytes read from a publisher, enforced while streaming. The
# fetch used to be unbounded — the whole body landed in memory before the
# content-type was even inspected — which is how a single oversized response
# could OOM-kill the 512MB instance. 1.5MB comfortably covers a news page whose
# article body sits behind a few hundred KB of inline scripts.
FULLTEXT_MAX_BYTES = int(os.environ.get("FULLTEXT_MAX_BYTES", "1500000"))
# Same unbounded-read hazard as above, but for the RSS fetch itself (Scout runs
# this over every feeds.yaml URL on every pipeline run — far more exposure than
# the per-story fulltext fetch). A misconfigured feed, a redirect to a large
# page, or a feed host serving something other than XML is enough to OOM-kill
# the 512MB instance before feedparser ever sees the bytes.
RSS_MAX_BYTES = int(os.environ.get("RSS_MAX_BYTES", "5000000"))
# Ceiling on how many articles ConnectionFinder pairs up. That stage compares
# every article against every other, so its cost is QUADRATIC in this number:
# 600 is ~180k pairs, while the unbounded 7-day window it used to read had grown
# to ~5,000 articles (~13 million pairs) and OOM-killed the instance on every
# run — the pipeline reached `trends` and never got to `stories`. Raise this
# only with the square in mind: doubling it quadruples the work.
CONNECTIONS_MAX_ARTICLES = int(os.environ.get("CONNECTIONS_MAX_ARTICLES", "600"))
# Hard cap on the merged-fact brief handed to the LLM (token control).
BRIEF_MAX_CHARS = int(os.environ.get("BRIEF_MAX_CHARS", "2600"))
# How long story_history is kept. It is the only append-only table in the
# schema, so it needs an explicit ceiling on a 512MB box. 90 days is well past
# the 7-day feed window while still covering "moved most this week" and the
# "since you saved it" comparisons on stories bookmarked months ago.
HISTORY_KEEP_DAYS = int(os.environ.get("HISTORY_KEEP_DAYS", "90"))
# How far corroboration must fall from a story's own peak before we tell the
# reader it weakened. Deliberately absolute rather than proportional: at the
# bottom of the scale a proportional test fires on rounding noise, and at the
# top it would never fire at all. 8 points is roughly one Verifier source
# dropping out — small enough to catch a real collapse early, large enough that
# a rescored-but-unchanged story stays quiet.
CORRECTION_MIN_DROP = float(os.environ.get("CORRECTION_MIN_DROP", "8"))
DB_PATH = str(ROOT / os.environ.get("DB_PATH", "newslens.db"))
FEEDS_FILE = ROOT / "feeds.yaml"
SOURCES_FILE = ROOT / "sources.yaml"
PROMPTS_FILE = ROOT / "prompts.yaml"
TICKERS_FILE = ROOT / "tickers.yaml"
SAMPLE_FILE = ROOT / "sample_articles.json"
ADMIN_PAGE = ROOT / "admin.html"

# ------------------------------------------------------- finance pipeline
# A second, domain-isolated pipeline over the finance/business beats. It writes
# only to fin_* tables, so every knob here is additive: with FINANCE_TOPICS
# empty it does nothing and the app behaves exactly as it did before.
FINANCE_TOPICS = [t.strip().lower() for t in os.environ.get(
    "FINANCE_TOPICS", "finance,business").split(",") if t.strip()]
# Runs as its own worker/cron by default (run_finance_pipeline.py). Set to 1 to
# also schedule it inside the API process — convenient locally, but on the
# 512MB box it puts a second LLM pipeline in the process that serves requests,
# which is exactly the coupling that made a ConnectionFinder OOM take the API
# down with it. See render-512mb-oom-limit.
FINANCE_IN_PROCESS = os.environ.get("FINANCE_IN_PROCESS", "0") == "1"
FINANCE_INTERVAL_HOURS = float(os.environ.get("FINANCE_INTERVAL_HOURS", "3"))
# Per-run ceilings. Every finance stage is bounded the same way the general
# pipeline's stages are — the ConnectionFinder OOM came from an unbounded pair
# scan, and none of these may be allowed to grow with the table.
FIN_MAX_STORIES_PER_RUN = int(os.environ.get("FIN_MAX_STORIES_PER_RUN", "12"))
FIN_MAX_ARTICLES_PER_RUN = int(os.environ.get("FIN_MAX_ARTICLES_PER_RUN", "400"))
FIN_MAX_TREND_STORIES = int(os.environ.get("FIN_MAX_TREND_STORIES", "60"))
FIN_MAX_FORECASTS_PER_RUN = int(os.environ.get("FIN_MAX_FORECASTS_PER_RUN", "8"))
FIN_WINDOW_DAYS = float(os.environ.get("FIN_WINDOW_DAYS", "7"))
# Rolling window Agent 2 links over. The spec's 7-30 days: wider than the news
# window because a cascade (rates -> defaults -> bank stress) plays out over
# weeks, and a 7-day view cannot see the first link any more.
FIN_TREND_WINDOW_DAYS = float(os.environ.get("FIN_TREND_WINDOW_DAYS", "30"))
# How far a cascade walk may travel from a seed entity in the KG. 3 hops covers
# "supplier of a supplier of the named company"; beyond that the mechanism is
# no longer something we can evidence from the reporting.
FIN_MAX_CASCADE_HOPS = int(os.environ.get("FIN_MAX_CASCADE_HOPS", "3"))
# KG edges older than this are pruned. Same reasoning as prune_history: an
# append-only table on a 512MB box needs a ceiling written down at birth.
FIN_KG_RETAIN_DAYS = float(os.environ.get("FIN_KG_RETAIN_DAYS", "120"))
FIN_FORECAST_RETIRE_DAYS = float(os.environ.get("FIN_FORECAST_RETIRE_DAYS", "30"))
