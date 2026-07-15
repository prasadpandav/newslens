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

# Tasks that need deep reasoning use each provider's stronger model when set.
# Empty string = that provider uses its base model for reasoning tasks too.
REASONING_TASKS = set(t.strip() for t in
                      os.environ.get("REASONING_TASKS", "signals,trend").split(","))
GROQ_REASONING_MODEL = os.environ.get("GROQ_REASONING_MODEL", "")
GEMINI_REASONING_MODEL = os.environ.get("GEMINI_REASONING_MODEL", "gemini-2.5-pro")
DEEPSEEK_REASONING_MODEL = os.environ.get("DEEPSEEK_REASONING_MODEL", "deepseek-v4-pro")
OPENAI_REASONING_MODEL = os.environ.get("OPENAI_REASONING_MODEL", "")
NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "")
PIPELINE_INTERVAL_HOURS = float(os.environ.get("PIPELINE_INTERVAL_HOURS", "3"))
# Max stories built per run — keeps a single run inside LLM budgets.
MAX_STORIES_PER_RUN = int(os.environ.get("MAX_STORIES_PER_RUN", "20"))
DB_PATH = str(ROOT / os.environ.get("DB_PATH", "newslens.db"))
FEEDS_FILE = ROOT / "feeds.yaml"
SOURCES_FILE = ROOT / "sources.yaml"
PROMPTS_FILE = ROOT / "prompts.yaml"
SAMPLE_FILE = ROOT / "sample_articles.json"
