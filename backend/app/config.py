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
PIPELINE_INTERVAL_HOURS = float(os.environ.get("PIPELINE_INTERVAL_HOURS", "3"))
# Max stories built per run — keeps a single run inside LLM budgets.
MAX_STORIES_PER_RUN = int(os.environ.get("MAX_STORIES_PER_RUN", "20"))

# Global cap on REAL LLM calls per rolling minute, across every task and provider.
# Protects free-tier RPM limits (Groq is 30/min) and bounds token burn. 0 = off.
LLM_MAX_CALLS_PER_MIN = int(os.environ.get("LLM_MAX_CALLS_PER_MIN", "30"))

# Near-duplicate article merging (same story from different sources). Articles
# whose titles cosine-match at/above this are grouped so the LLM stages process
# the event ONCE (with all sources annotated) instead of once per source.
DEDUPE_SIMILARITY = float(os.environ.get("DEDUPE_SIMILARITY", "0.62"))
DEDUPE_WINDOW_DAYS = float(os.environ.get("DEDUPE_WINDOW_DAYS", "7"))
DB_PATH = str(ROOT / os.environ.get("DB_PATH", "newslens.db"))
FEEDS_FILE = ROOT / "feeds.yaml"
SOURCES_FILE = ROOT / "sources.yaml"
PROMPTS_FILE = ROOT / "prompts.yaml"
SAMPLE_FILE = ROOT / "sample_articles.json"
ADMIN_PAGE = ROOT / "admin.html"
