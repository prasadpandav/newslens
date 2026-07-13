# NewsLens iOS — Design Philosophy v2 ("Bluelligent Native")

The web portal defined the brand: *convert information overload into understanding*.
This document translates that philosophy into a **native iOS** design system — not a port
of the web CSS, but the same soul expressed through Apple's material language.

## 1. Principles (carried over + upgraded)

1. **Understanding over consumption.** Progress = comprehension (understanding meter,
   "story understood" moments), never time-on-site or infinite scroll.
2. **Trust is a first-class citizen.** Corroboration ring + rationale on every story;
   claim-by-claim verdicts; AI-inferred links always labelled as hypotheses.
3. **Calm intelligence.** Dark-first ink palette, one accent gradient, semantic color
   only where it means something (trust/warning/disputed/AI).
4. **Native, not webby.** Liquid Glass materials, SF type + New York serif, spring
   physics, haptics, zoom hero transitions — the app should feel inevitable on iOS.

## 2. What's new in iOS design (2026) and how we adopt it

| Trend / API | Our use |
|---|---|
| **Liquid Glass** (`glassEffect`, iOS 26) — dynamic material for the *functional layer* only, never content | Ask-AI floating button, topic filter bar, understanding pill. Content cards stay opaque-dark (glass must have content scrolling beneath it). Fallback: `.ultraThinMaterial` on iOS 17–25. |
| `GlassEffectContainer` + morphing | Groups the floating controls so glass blends and morphs between states. |
| **Zoom hero transitions** (`matchedTransitionSource` + `navigationTransition(.zoom)`, iOS 18+) | Story card → story reader zooms from the card, keeping context. |
| **Scroll-driven motion** (`scrollTransition`, iOS 17) | Brief cards fade/rise/scale as they enter; subtle parallax on the hero. |
| **Sensory feedback** (`.sensoryFeedback`) | Soft impact when a module opens; success haptic on "story understood". |
| Semantic tinting, not decoration | Tint only the primary action and semantic states, per Liquid Glass guidance. |
| Reduced motion & accessibility | All custom motion respects `accessibilityReduceMotion`; system handles glass legibility. |

## 3. Design tokens (Theme.swift)

- **Palette** — Ink `#070B14` base, surface white@4–6%, accent `#4D9FFF`, AI violet
  `#7C5CFF` (gradient accent→violet), trust `#3DDC97`, warning `#FFC24D`,
  disputed/breaking `#FF5D73`, prediction `#B48CFF`. Dark-mode first; light mode maps
  the same semantics onto paper tones.
- **Typography** — Editorial headlines: system serif (New York) via `.fontDesign(.serif)`,
  large sizes with tight leading. UI: SF Pro (default). Data: SF Mono via
  `.monospaced()`. Dynamic Type throughout.
- **Shape** — radii 18/12/8; hairline borders white@8%; continuous corner curves.
- **Motion** — one spring: `response 0.45, damping 0.85`; module expansion uses it;
  staggered card entrance ≤ 6 items; everything ≤ 500 ms; no idle animation.
- **Icons** — SF Symbols only (semantic, hierarchical rendering), matching the web's
  icon meanings (clock=what, chart=bigger picture, shield=claims, sparkles=AI).

## 4. Screen-by-screen upgrade

- **Onboarding → "Calibrate your lens."** Paged flow rebuilt with animated gradient
  orbs, chip grids, progress dots, spring page transitions. Same optional-everything
  data model.
- **Feed → Daily Brief.** Greeting + date + streak; "N stories · M min · top signal"
  intelligence summary; glass topic filter; story cards with serif headlines, trust
  meter, impact badges; scroll transitions; zoom into stories; Early-Signals strip
  (micro-trends) and "Your intelligence" stats card.
- **Story → Understanding journey.** Serif hero, corroboration ring with rationale,
  gradient "What this means for you" card, expandable modules (What happened / Why it
  matters / Bigger picture / Hidden connections / Claim check / Sources) driving a
  sticky understanding pill; haptic + toast at 100%.
- **New: Trend Radar tab.** Macro trends and 72-hour early signals with native
  sparklines (Canvas), velocity badges.
- **New: Ask AI.** Floating glass button opens a sheet chat (uses `/ask`), suggestion
  chips, story-aware context.

## 5. Engagement (educational, never dopamine)

Learning streak, stories understood, topics explored — all local, all framed as
learning. No badges, no red dots, no infinite feed. Notification philosophy unchanged:
only high-impact, personally relevant events.
