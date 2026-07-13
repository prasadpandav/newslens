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
NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "")
PIPELINE_INTERVAL_HOURS = float(os.environ.get("PIPELINE_INTERVAL_HOURS", "3"))
DB_PATH = str(ROOT / os.environ.get("DB_PATH", "newslens.db"))
FEEDS_FILE = ROOT / "feeds.yaml"
SOURCES_FILE = ROOT / "sources.yaml"
PROMPTS_FILE = ROOT / "prompts.yaml"
SAMPLE_FILE = ROOT / "sample_articles.json"
