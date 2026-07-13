# NewsLens

Personalized news that explains what each story means for **you**. Three parts:

- `ARCHITECTURE.md` — full system blueprint (read this first)
- `backend/` — working agentic backend (Python/FastAPI), tested end-to-end
- `ios/NewsLens/` — SwiftUI app source (open in Xcode)
- `web/index.html` — **Bluelligent web portal** (open in any browser)

## Web portal

One self-contained file: `web/index.html`. Double-click to open, or serve it
(`python -m http.server 3000` inside `web/`). It talks to the backend at
`http://localhost:8000`; if the backend is offline it switches to built-in demo
data and shows a "demo data" badge, so it always demos well.

Includes: landing page, personalized Daily Brief, story reader with modular
expandable cards (what happened / why it matters / bigger picture / hidden
connections / claim check / sources), an "understanding meter", Trend Radar
(macro + 72h early signals + story network graph), ⌘K natural-language search,
an Ask-AI assistant on every story, and a learning-framed engagement layer
(streaks, stories understood, topics explored). Dark-mode-first design system,
reduced-motion support, keyboard navigation.

To point it at a deployed backend: in the browser console run
`localStorage.setItem('bl_api','https://your-server')` and reload.

## Run the backend (5 minutes)

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env          # works with NO keys (mock mode)
python run_pipeline.py        # fetch news -> trends -> connections -> verify -> stories
uvicorn app.main:app --port 8000
```

Then try it:

```bash
curl -X POST localhost:8000/users
# -> {"user_id": "...", "token": "..."}
curl -X PUT localhost:8000/users/<user_id>/context \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"interests":["technology"],"profession":"pharmacist","location":{"city":"Pune","country":"India"}}'
curl -X POST localhost:8000/admin/run
curl "localhost:8000/feed?user_id=<user_id>" -H "Authorization: Bearer <token>"
```

**Go live with real AI:** get a free Groq key (console.groq.com) and/or Gemini key
(aistudio.google.com), put them in `.env`, set `LLM_PROVIDER=auto`. Nothing else changes.

**Tune the product without coding:** edit `feeds.yaml` (news sources), `prompts.yaml`
(all agent instructions, in English), `sources.yaml` (credibility tiers).

## Run the iOS app (redesigned — "Bluelligent Native")

The app now shares the web portal's design language, adapted natively: dark-first ink
palette, New York serif headlines, Liquid Glass on iOS 26 (material fallback on 17–25),
zoom hero transitions (iOS 18+), scroll-driven card motion, haptics, understanding
meter, Trend Radar tab, and an Ask-AI sheet. See `ios/DESIGN-IOS.md` for the full
design system.

1. Xcode → New Project → iOS App, name `NewsLens`, interface SwiftUI, iOS 17+.
2. Delete the generated `ContentView.swift`; drag the 8 files from `ios/NewsLens/` into the project.
3. If testing on the Simulator with a local backend, `http://localhost:8000` works as-is;
   on a real device, set `baseURL` in `APIClient.swift` to your Mac's/server's IP, and add an
   App Transport Security exception for plain HTTP (or use HTTPS on the server).
4. Run. Complete onboarding → feed appears (run the pipeline first so stories exist).

## What the backend agents do

Scout (RSS fetch, sample-data fallback) → EntityTagger → TrendLinker (macro trends) →
MicroTrendDetector (accelerating 72h signals) → ConnectionFinder (hidden cross-story links)
→ Verifier (claim extraction + corroboration score) → Storyteller (narrative + catchy-true
headline) → Personalizer ("what this means for you", per user). The Orchestrator plans
topics from all users' interests and runs the stages every `PIPELINE_INTERVAL_HOURS`.

## Deploy free

`docker build -t newslens backend && docker run -d -p 8000:8000 newslens` on an
Oracle Cloud Always-Free VM (or Fly.io/Render free tier). See ARCHITECTURE.md §8.
# newslens
