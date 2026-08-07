"""The finance pipeline: a domain-isolated sibling of the general news
pipeline, tuned for financial and business reporting.

Deliberately additive. It reuses the general pipeline's ingestion (Scout),
near-duplicate grouping (Deduper), source tiering (Verifier), multi-source
merge (textmerge.build_brief), image pick and LLM client verbatim — everything
that is about *news* rather than about *finance*. What it adds is the finance
part: quantitative extraction with provenance, ticker resolution against a
fixed map, multi-actor sentiment, a knowledge graph, cascade linking over that
graph, and scenario forecasting under a no-advice constraint.

It writes ONLY to fin_* tables. Nothing here reads or writes `stories`,
`trends`, `signals` or `feed_items`, so the app keeps working exactly as it
does today whether or not this pipeline has ever run.
"""
