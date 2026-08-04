"""Configuration. Reads .env if present, else environment, else safe defaults."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _load_dotenv():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

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
REASONING_TASKS = set(t.strip() for t in
                      os.environ.get("REASONING_TASKS", "signals,signals_unit,trend").split(","))
# Under LLM_PROVIDER=auto, reasoning tasks (trend/forecast) try these providers
# in order — strongest thinking model first — falling through on missing key or
# rate-limit. Ordinary tasks keep the cheaper free-first order below.
REASONING_PROVIDER_ORDER = [p.strip() for p in os.environ.get(
    "REASONING_PROVIDER_ORDER", "deepseek,groq,gemini,openai").split(",") if p.strip()]
GROQ_REASONING_MODEL = os.environ.get("GROQ_REASONING_MODEL", "")
GEMINI_REASONING_MODEL = os.environ.get("GEMINI_REASONING_MODEL", "gemini-2.5-flash")
DEEPSEEK_REASONING_MODEL = os.environ.get("DEEPSEEK_REASONING_MODEL", "deepseek-v4-pro")
OPENAI_REASONING_MODEL = os.environ.get("OPENAI_REASONING_MODEL", "")
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
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
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

# Near-duplicate article merging (same story from different sources). Articles
# whose titles cosine-match at/above this are grouped so the LLM stages process
# the event ONCE (with all sources annotated) instead of once per source.
DEDUPE_SIMILARITY = float(os.environ.get("DEDUPE_SIMILARITY", "0.62"))
DEDUPE_WINDOW_DAYS = float(os.environ.get("DEDUPE_WINDOW_DAYS", "7"))

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
SAMPLE_FILE = ROOT / "sample_articles.json"
ADMIN_PAGE = ROOT / "admin.html"
