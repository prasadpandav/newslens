# NewsLens — Architecture Blueprint

*Personalized news that explains what it means for **you**.*

Version 1.0 · July 2026 · Target: small beta (10–100 users) · Cost target: $0/month

---

## 1. Product Overview

NewsLens is an iOS app + agentic backend. The backend continuously ingests news, links stories into macro and micro trends, surfaces hidden connections between seemingly unrelated stories, scores the credibility of claims, and rewrites every story as a narrative with a catchy-but-true headline. The app then personalizes each story against the user's saved context: "here's the news, and here's what it means for your job, your city, your money."

## 2. High-Level System

```
┌──────────────┐   HTTPS/JSON    ┌────────────────────────────────────────┐
│  iOS App     │ ◄─────────────► │  FastAPI Backend (single container)     │
│  (SwiftUI)   │                 │                                        │
│              │                 │  ┌──────────────────────────────────┐  │
│ Onboarding → │  POST /context  │  │ ORCHESTRATOR (Planner + Runner)  │  │
│ user context │                 │  │                                  │  │
│              │                 │  │ Scout → Trends → Micro-Trends →  │  │
│ Feed / Story │  GET /feed      │  │ Connections → Verifier →         │  │
│ Detail       │  GET /story/:id │  │ Storyteller → Personalizer       │  │
└──────────────┘                 │  └──────────────────────────────────┘  │
                                 │  SQLite DB · APScheduler (cron)        │
                                 └───────────┬────────────────────────────┘
                                             │
                    ┌────────────────────────┼─────────────────────┐
                    ▼                        ▼                     ▼
             RSS feeds (free,          GDELT (free,          Groq / Gemini
             unlimited)                unlimited)             free-tier LLM APIs
```

One process, one database file, one scheduled job. Deliberately boring so one person with minimal technical knowledge can run it.

## 3. User Context (the personalization core)

Captured in the iOS onboarding journey, stored in the backend, and injected into the Personalizer agent's prompt for every story.

```json
{
  "user_id": "uuid",
  "interests": ["technology", "energy", "cricket"],
  "profession": "pharmacist",
  "line_of_business": "retail pharmacy chain, 3 stores",
  "role_seniority": "owner",
  "location": {"city": "Pune", "region": "Maharashtra", "country": "India"},
  "native_language": "Marathi",
  "preferred_language": "English",
  "micro": {
    "commute": "drives 40 min daily",
    "family": "two school-age children",
    "financial_exposure": ["mutual funds", "commercial property lease"],
    "supply_dependencies": ["pharma distributors", "imported generics"],
    "goals": ["expand to a 4th store in 2 years"]
  },
  "reading_level": "expert_in_domain_simple_elsewhere",
  "updated_at": "..."
}
```

Design rules: every field is optional; onboarding asks conversationally (chips + free text, not forms); `micro` is an open key–value bag so new parameters need no schema migration; users can edit context any time (Profile tab). Over time, implicit signals (taps, dwell time, "more like this") append to `micro`.

## 4. Agent Design

All agents are plain Python classes sharing one interface (`run(input) -> output`), one LLM client, and structured JSON outputs. No heavy agent framework — this is a deliberate maintainability choice; the orchestration graph is simple enough that LangGraph/CrewAI would add dependencies without adding capability. (If the pipeline later needs branching, retries with human-in-the-loop, or parallel fan-out per user, LangGraph is the recommended upgrade — it's free and open source.)

### 4.1 Orchestrator (Planner + Runner)
- **Planner**: given the current DB state and the set of active user contexts, produces a run plan: which topics to fetch (union of all users' interests + baseline general news), which stages to run, batch sizes tuned to free-tier rate limits.
- **Runner**: executes the plan as a DAG, with per-stage checkpointing to SQLite so a crash resumes instead of restarting, exponential backoff on LLM 429s, and automatic failover between LLM providers.
- Runs on a schedule (default: every 3 hours) via APScheduler — no external queue needed at beta scale.

### 4.2 Scout (news fetcher)
- **Tools**: RSS/Atom fetcher (feedparser) over a curated, per-topic feed registry (world, business, tech, science, health, sports, regional feeds per user location); optional GDELT DOC 2.0 API for topic queries; optional NewsData.io (200 req/day free) for gaps.
- Deduplicates by URL hash + title similarity; extracts title, summary, source, published time, topic tags. Stores raw articles.
- RSS is the primary source because it is genuinely unlimited and free; the API keys are optional enhancers.

### 4.3 Trend Linker (macro trends)
- Embeds article title+summary (sentence-transformers `all-MiniLM-L6-v2`, runs locally, free; TF-IDF fallback where the model can't run) and clusters (agglomerative, cosine threshold).
- For each cluster ≥3 articles, an LLM call names the trend, writes a 2-sentence trend narrative, and tags affected sectors + regions — this is what lets a global macro trend ("semiconductor export controls") be linked to a local area ("Pune electronics manufacturers").

### 4.4 Micro-Trend Detector
- Same clustering at a tighter similarity threshold and shorter time window (72h), restricted within a sector or region, and compared against the previous window: a micro-trend is a small cluster whose article velocity is accelerating. LLM names it and states what early signal it represents.

### 4.5 Hidden-Connections Agent
- Finds pairs of stories that are **not** in the same cluster (low surface similarity) but share second-order links. Two passes:
  1. Cheap graph pass: shared entities/sectors/regions extracted at ingest (LLM entity extraction, batched).
  2. LLM reasoning pass on candidate pairs: "Story A and Story B look unrelated. Trace any causal or economic chain between them. Answer with the chain, confidence 0–1, and who is affected." Pairs above confidence 0.6 are stored as `connections` with the explanation chain.

### 4.6 Verifier (claim credibility)
- Extracts the 1–5 checkable claims per story (LLM).
- For each claim: cross-source corroboration (how many independent ingested sources report it), source-reputation prior (small editable YAML of outlet tiers), internal consistency check, and freshness.
- Outputs per-claim verdicts and a story-level **credibility score 0–100%** with a one-line rationale ("Reported by 6 independent outlets incl. 2 wire services; casualty figure unconfirmed"). Shown in-app as a meter — always with the rationale, never a bare number, and labelled "corroboration score", because this measures corroboration, not truth.

### 4.7 Storyteller
- Rewrites each story cluster as a narrative: hook → what happened → why now → what's next. Constraint-checked headline: an LLM writes a catchy headline, then a second "headline-integrity" check verifies every factual token in the headline appears in the verified claims; failing headlines are regenerated. Catchy **and** true, enforced rather than hoped for.

### 4.8 Personalizer
- The only per-user stage. For each user × relevant story: takes the story, its trends/connections/credibility, and the full user context, and produces "What this means for you" (2–4 sentences), an impact score (0–3: none/low/medium/high) used for feed ranking, and optionally a native-language rendering of the summary.
- Cost control: only stories matching user interests/location/sector run through this stage; everything else gets the generic narrative.

### 4.9 Shared guardrails
- Every agent output is schema-validated (Pydantic); invalid JSON → one retry with the validation error in the prompt → then skip and log.
- Every LLM call is logged with tokens used, so free-tier budget is observable at `/admin/usage`.

## 5. Pipeline (one orchestrator run)

```
Scout ──► Entity extraction ──► Trend Linker ──► Micro-Trends
                 │                      │
                 └────► Hidden Connections ◄────┘
                                │
                          Verifier
                                │
                          Storyteller
                                │
                          Personalizer (per user)
                                │
                          Feed rows in SQLite
```

Each stage reads/writes SQLite tables, so stages are independently re-runnable and debuggable by inspecting the DB with any SQLite browser.

## 6. Data Model (SQLite)

| Table | Purpose |
|---|---|
| `users` | user_id, context JSON, device token |
| `articles` | raw ingested articles, embedding blob, entities JSON |
| `trends` | macro trends: name, narrative, sector/region tags, member article ids |
| `micro_trends` | window, velocity, signal statement |
| `connections` | article_a, article_b, chain explanation, confidence |
| `claims` | story_id, claim text, verdict, evidence JSON |
| `stories` | narrative, headline, credibility %, linked trend/connection ids |
| `feed_items` | user_id × story_id, personal impact text, impact score, rank |
| `runs` | orchestrator checkpoints + LLM usage log |

SQLite is correct for 10–100 users (reads are tiny, writes are batch). Migration path: the schema is plain SQL → Postgres (Supabase free tier) when concurrent writes or multi-device sync demand it.

## 7. API (FastAPI, JSON)

- `POST /users` → create user, returns id + token
- `PUT /users/{id}/context` → save/update context (onboarding + edits)
- `GET /feed?user_id=` → ranked personalized feed
- `GET /story/{id}?user_id=` → full story: narrative, "for you", trends, connections, claim-by-claim verification
- `POST /feedback` → taps/likes/hide → implicit context signals
- `GET /admin/usage` → LLM budget dashboard
- Auth for beta: per-user bearer token issued at signup (no passwords; device-bound). Upgrade path: Supabase Auth.

## 8. Stack (all free / open source)

| Layer | Choice | Why |
|---|---|---|
| iOS | SwiftUI, iOS 17+ | native, no cross-platform toolchain to maintain |
| API | Python 3.11 + FastAPI + Uvicorn | most tutorial-rich stack; easiest for a low-code maintainer |
| DB | SQLite (file) → Supabase Postgres later | zero admin |
| Scheduler | APScheduler in-process | no cron, no queue infra |
| News | RSS via feedparser (primary), GDELT DOC API, NewsData.io free tier (optional) | RSS + GDELT are unlimited and free |
| Embeddings | sentence-transformers MiniLM (local, free) or TF-IDF fallback | no per-call cost |
| LLM | **Groq free tier** (llama-3.3-70b: 30 RPM / 14.4k req-day, low TPM) primary; **Gemini Flash-Lite free tier** (250k TPM, ~1k req/day) failover | complementary limits: Groq gives request volume, Gemini gives token volume; client auto-switches on 429 |
| Hosting | Oracle Cloud Always-Free VM (best: truly always free, 24GB RAM ARM) or Fly.io/Render free tier | $0 |
| Deploy | single Dockerfile, `docker compose up -d` | one command |
| Monitoring | `/admin/usage` + UptimeRobot free ping | enough for beta |

**Free-tier budget math (beta):** ~40 topics × 8h cycle ≈ 500 articles/day → after clustering ≈ 60 stories/day → ~6 LLM calls per story + 100 users × ~10 personalizations/day ≈ **1,400 LLM calls/day** — comfortably inside Groq's 14.4k/day, with Gemini absorbing long-context calls (trend summaries) that exceed Groq's 6k TPM.

## 9. Maintainability for a non-expert

- One repo, one container, one DB file, one config file (`.env`).
- Feed list, source-reputation tiers, and prompts live in editable text/YAML files — tuning the product requires editing English, not Python.
- Mock mode (`LLM_PROVIDER=mock`) runs the whole pipeline with no keys, so you can always test safely.
- Every stage is re-runnable: `python run_pipeline.py --stage verifier`.

## 10. Risks & mitigations

- **Free-tier changes** (Google cut quotas 50–80% in Dec 2025): provider-agnostic LLM client; adding a new provider is one small file.
- **News API ToS**: free API tiers forbid commercial use — fine for beta; RSS/GDELT carry no such limit. Store only headline/summary/link, deep-link to publishers (also keeps you copyright-safe).
- **Verification over-trust**: the score is corroboration, not truth — UI always shows rationale + sources, never a bare %.
- **Hallucinated connections**: hidden-connection chains are labelled "AI-inferred hypothesis" in the UI, with confidence shown.
- **App Store review**: news apps with AI summaries should disclose AI generation (label on every story) — required by policy and good practice.

## 11. Roadmap

1. **Now (this repo)**: backend prototype in mock mode + SwiftUI app against local backend.
2. **Week 1–2**: real Groq/Gemini keys, curate 40–60 RSS feeds, deploy to Oracle free VM, TestFlight.
3. **Beta**: feedback loop → implicit context signals; push notifications (APNs, free) for high-impact stories.
4. **Later**: Supabase (auth + Postgres), native-language full stories, LangGraph if the pipeline grows branches.
