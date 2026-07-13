"""Provider-agnostic LLM client.

Providers: mock (no keys, heuristic answers so the whole pipeline runs),
groq, gemini, auto (groq first, gemini on rate-limit/failure).
Every call asks for JSON and validates it; one retry with the error appended.
Usage (calls + rough tokens) is accumulated in `usage` for /admin/usage.
"""
import json
import re
import time
import httpx
from . import config

usage = {"calls": 0, "tokens": 0, "provider_calls": {}, "rate_limited": 0, "failed": 0}

# Pace calls to stay under free-tier requests-per-minute limits.
_last_call: dict[str, float] = {}
MIN_INTERVAL = {"groq": 2.1, "gemini": 6.5}   # seconds between calls per provider

# When a provider rate-limits us, bench it for a while instead of knocking on
# its door for every call. It gets retried automatically after the cooldown.
_benched_until: dict[str, float] = {}
COOLDOWN_SECONDS = 900  # 15 min


def _pace(provider):
    wait = MIN_INTERVAL.get(provider, 0) - (time.time() - _last_call.get(provider, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[provider] = time.time()


def complete_json(task: str, prompt: str, retries: int = 1):
    """Return parsed JSON from the LLM, or None if every provider failed.

    IMPORTANT: with a real provider configured, failure returns None and the
    caller skips that item (it is retried on the next pipeline run). Mock
    content is only ever produced when LLM_PROVIDER=mock.
    """
    provider = config.LLM_PROVIDER
    if provider == "mock":
        return _mock(task, prompt)
    order = {"groq": ["groq"], "gemini": ["gemini"], "auto": ["groq", "gemini"]}.get(
        provider, ["groq", "gemini"])
    last_err = None
    for attempt in range(retries + 1):
        for p in order:
            if time.time() < _benched_until.get(p, 0):
                continue  # provider is cooling down after a rate limit
            try:
                _pace(p)
                text = _call(p, prompt if attempt == 0 else
                             f"{prompt}\n\nYour previous answer was invalid JSON ({last_err}). "
                             f"Reply with ONLY valid JSON.")
                _benched_until.pop(p, None)
                return _extract_json(text)
            except RateLimited:
                usage["rate_limited"] += 1
                _benched_until[p] = time.time() + COOLDOWN_SECONDS
                continue
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:200]
                continue
    usage["failed"] += 1
    return None


class RateLimited(Exception):
    pass


def _record(provider, tokens):
    usage["calls"] += 1
    usage["tokens"] += tokens
    usage["provider_calls"][provider] = usage["provider_calls"].get(provider, 0) + 1


def _call(provider, prompt):
    if provider == "groq":
        if not config.GROQ_API_KEY:
            raise RuntimeError("no groq key")
        r = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={"model": config.GROQ_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.4,
                  "response_format": {"type": "json_object"}},
            timeout=60)
        if r.status_code == 429:
            raise RateLimited()
        r.raise_for_status()
        data = r.json()
        _record("groq", data.get("usage", {}).get("total_tokens", 0))
        return data["choices"][0]["message"]["content"]
    if provider == "gemini":
        if not config.GEMINI_API_KEY:
            raise RuntimeError("no gemini key")
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": config.GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json",
                                       "temperature": 0.4}},
            timeout=60)
        if r.status_code == 429:
            raise RateLimited()
        r.raise_for_status()
        data = r.json()
        toks = data.get("usageMetadata", {}).get("totalTokenCount", 0)
        _record("gemini", toks)
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
                "natural follow profile intelligence").split())
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", prompt)]
    freq = {}
    for w in words:
        if w not in stop:
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:n]]


def _mock(task, prompt):
    _record("mock", 0)
    kws = _keywords(prompt)
    label = " ".join(k.capitalize() for k in kws[:2]) or "General News"
    if task == "entities":
        return {"entities": kws, "sectors": kws[:1], "regions": []}
    if task == "trend":
        return {"name": f"{label} gathers momentum",
                "narrative": f"Multiple outlets are converging on {label.lower()}, "
                             f"suggesting sustained attention in this area.",
                "sectors": kws[:2], "regions": []}
    if task == "micro_trend":
        return {"name": f"{label} accelerating",
                "signal": f"Coverage of {label.lower()} is picking up over the last "
                          f"72 hours — worth watching."}
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
                "narrative": f"Reports centred on {label.lower()} emerged across several "
                             f"sources today. The core development is summarised in the "
                             f"linked articles, and momentum has been building in this "
                             f"space. Watch for follow-up coverage and official responses "
                             f"in the coming days. (Placeholder text — this story was "
                             f"generated without an LLM key or after hitting a rate limit.)"}
    if task == "signals":
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
    if task == "personalize":
        return {"impact_text": f"Given your profile, developments in {label.lower()} "
                               f"could affect your sector or region. Worth a closer read.",
                "impact_score": 1}
    return {}
