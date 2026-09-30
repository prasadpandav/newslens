"""Which beat a story is filed under, beyond "whichever feed saw it first".

An article's topic is the feeds.yaml key of the feed it arrived through. That
is fine for most beats, but AI coverage mostly arrives through general
technology feeds (The Verge, TechCrunch, Ars, MIT Tech Review), and several of
the `ai` feeds are sub-feeds of those same sites, so the same URL is usually
stored under `technology` first and the `ai` copy is dropped as a duplicate.
On 2026-10-01 production had 25 AI stories and 2 of them were filed under AI,
so the reader saw "many AI stories" and no AI chip.

`refine` files a technology or science article under `ai` when its TITLE is
plainly about AI. Titles only: summaries mention AI in passing far too often.
Finance and business are never refined; an AI-spending story keeps its money
beat. `ai` is recorded as a narrower beat of `technology` (PARENT), so a reader
who follows Technology still has AI stories count as theirs.
"""
import re
import time

# Narrower beat -> the broader beat it belongs to.
PARENT = {"ai": "technology"}
# Feed topics an article may be moved out of.
REFINABLE = ("technology", "science")

# Case-sensitive: "AI" the acronym, not "ai" inside a word or the surname Ai.
_AI_CASED = re.compile(r"\b(AI|A\.I\.|AGI|LLMs?|GPT-?\d[\w.]*|xAI|GenAI|genAI)\b")
# Names and phrases that are unambiguous in a technology headline. Not
# "Copilot" (Microsoft puts it on keyboards and mice), not "Gemini" or "Claude"
# on their own (a Gemini AI story says "AI" anyway).
_AI_WORDS = re.compile(
    r"\b(artificial intelligence|generative|large language models?|machine learning|"
    r"deep learning|neural networks?|chatbots?|openai|chatgpt|anthropic|deepmind|"
    r"deepseek|hugging ?face|midjourney)\b", re.I)


def is_ai(title):
    t = title or ""
    return bool(_AI_CASED.search(t) or _AI_WORDS.search(t))


def refine(topic, title):
    """The topic to file an article or story under."""
    return "ai" if topic in REFINABLE and is_ai(title) else topic


def matches(topic, interests):
    """Does a story on `topic` belong to a reader who follows `interests`
    (a set of lower-cased interests)? A narrower beat counts for its parent."""
    t = (topic or "").lower()
    return bool(t) and (t in interests or PARENT.get(t, "") in interests)


def with_parent(topic):
    """The topic plus its parent, as text, for word-matching code."""
    t = (topic or "").lower()
    return f"{t} {PARENT[t]}" if t in PARENT else t


def backfill(con, days=14):
    """Re-file recent articles and stories that `refine` would place
    differently. Idempotent and cheap (only the refinable topics, only recent
    rows), so it runs at every pipeline start, and changes nothing once the
    catalogue is consistent. Returns (articles, stories) moved."""
    since = time.time() - days * 86400
    marks = ",".join("?" * len(REFINABLE))
    arts = [r["id"] for r in con.execute(
        f"SELECT id, title FROM articles WHERE topic IN ({marks}) AND fetched_at > ?",
        (*REFINABLE, since)) if is_ai(r["title"])]
    stories = [r["id"] for r in con.execute(
        f"SELECT id, headline FROM stories WHERE topic IN ({marks}) AND updated_at > ?",
        (*REFINABLE, since)) if is_ai(r["headline"])]
    for table, ids in (("articles", arts), ("stories", stories)):
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            con.execute(f"UPDATE {table} SET topic='ai' WHERE id IN "
                        f"({','.join('?' * len(chunk))})", chunk)
    con.commit()
    return len(arts), len(stories)
