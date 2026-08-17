"""The three finance agents.

Everything about *news* is borrowed from the general pipeline rather than
rewritten: Scout has already fetched and Deduper has already grouped these
articles, Verifier tiers the sources and scores corroboration, textmerge merges
the group into one attributed brief, images picks the artwork, and the beat /
anchor cleaning that makes a story navigable is imported outright. What is
written here is only the finance part.

Call budget per run, all bounded by config:
  Agent 1  5 calls per event   (claims, verify, fin_extract, fin_sentiment, fin_story)
  Agent 2  1 call per run
  Agent 3  1 call per trend forecast (+1 only when a repair is needed)
"""
import re

from pydantic import ValidationError

from .. import config, db, fulltext, images, llm, textmerge
from ..agents import (PROMPTS, Verifier, clean_anchors, clean_beats, cosine,
                      depth_hint, _containment, _tf)
from ..schemas.finance import (SCHEMA_VERSION, ActorRole, ActorView, Basis,
                               Direction, EntityRow, EntityTable, EntityType,
                               Evidence, EventType, FinancialStory,
                               FinancialTrend, Horizon, ImpactLink, Invalidator,
                               KGRelationship, Metric, Scenario,
                               ScenarioForecast, ScenarioKind, SentimentProfile,
                               contains_advice)
from . import kg
from . import tickers as tk


def prompt(_key, **kw):
    """Same helper as the general pipeline, over the same prompts.yaml — the
    finance prompts just live under fin_* keys.

    The parameter is `_key` rather than `name` because the fin_forecast
    template has a {name} placeholder of its own, and a plain `name` parameter
    collides with it on the call."""
    return PROMPTS[_key].format(**kw)


def _enum(cls, value, default):
    """Coerce an LLM string to an enum, falling back rather than failing. A
    model that answers "M&A" instead of "merger_acquisition" should cost us the
    label, not the whole extraction."""
    if isinstance(value, cls):
        return value
    try:
        return cls(str(value).strip().casefold().replace(" ", "_").replace("&", "and"))
    except (ValueError, AttributeError):
        return default


def _num(v):
    """Floats out of whatever the model sent: 1234, "1,234.5", "Rs 8,500"."""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v or ""))
    return float(m.group(0).replace(",", "")) if m else None


# --------------------------------------------------------------- Agent 1
class FinancialStoryAgent:
    """Quantitative extraction + tabular CoT entities + SimTom sentiment + KG,
    synthesised into one financial story per EVENT.

    One story is one event, not one theme — the same rule the general
    Storyteller arrived at after a macro trend produced a 234-article blob. The
    event key is the group_id the Deduper already assigned, so a story updates
    in place as new outlets carry it.
    """

    MAX_PER_RUN = config.FIN_MAX_STORIES_PER_RUN

    def run(self, con, verifier: Verifier):
        window = db.now() - config.FIN_WINDOW_DAYS * 86400
        topics = config.FINANCE_TOPICS
        if not topics:
            db.log_run(con, "fin_stories", "ok", "FINANCE_TOPICS empty — nothing to do")
            return 0
        marks = ",".join("?" * len(topics))
        rows = con.execute(
            f"SELECT id, group_id FROM articles WHERE topic IN ({marks}) "
            f"AND fetched_at > ? ORDER BY fetched_at DESC LIMIT ?",
            (*topics, window, config.FIN_MAX_ARTICLES_PER_RUN)).fetchall()
        if not rows:
            db.log_run(con, "fin_stories", "ok", "no finance/business articles in window")
            return 0
        by_event = {}
        for r in rows:
            by_event.setdefault(r["group_id"] or r["id"], set()).add(r["id"])

        prior = [{"id": r["id"], "eid": r["event_id"],
                  "aids": set(db.uj(r["article_ids"], []))}
                 for r in con.execute(
                     "SELECT id, event_id, article_ids FROM fin_stories "
                     "WHERE updated_at > ?",
                     (db.now() - config.STORYTELLER_HISTORY_DAYS * 86400,)).fetchall()]

        new_ct = upd_ct = skipped = failed = errors = 0
        last_error = ""
        for event_id, ids in by_event.items():
            if new_ct + upd_ct >= self.MAX_PER_RUN:
                break
            try:
                r = self._one(con, event_id, sorted(ids), prior, verifier)
            except Exception as e:                              # noqa: BLE001
                # Per-EVENT isolation. Without it one malformed model answer
                # takes the whole stage down and every remaining event in the
                # run is lost — which is exactly what
                # "'list' object has no attribute 'get'" did in production, for
                # days, because the bad answer was also cached. The same shape
                # of bug had already cost us the entities stage once.
                errors += 1
                last_error = f"{type(e).__name__}: {e}"[:200]
                llm._note("event_failed", "finance", "fin_stories", last_error)
                try:
                    con.rollback()      # never leave a half-written story behind
                except Exception:       # noqa: BLE001
                    pass
                continue
            if r == "skipped":
                skipped += 1
            elif r == "failed":
                failed += 1
            elif r == "new":
                new_ct += 1
            elif r == "updated":
                upd_ct += 1
        detail = (f"{new_ct} new + {upd_ct} updated finance stories over "
                  f"{len(by_event)} events ({skipped} unchanged, {failed} failed"
                  + (f", {errors} errored — last: {last_error}" if errors else "")
                  + ")")
        # An error rate this high is a broken stage wearing a green badge, so it
        # is reported as one rather than hidden inside an "ok" detail string.
        db.log_run(con, "fin_stories", "error" if errors > max(1, new_ct + upd_ct)
                   else "ok", detail)
        return new_ct + upd_ct

    def _one(self, con, event_id, ids, prior, verifier):
        """One event, start to finish.

        Returns "skipped" | "failed" | "new" | "updated" so `run` can count
        outcomes without also owning failure handling — which is what lets the
        caller wrap exactly this in one try/except and lose a single event
        instead of the stage.
        """
        mine = next((p for p in prior if p["eid"] == event_id), None)
        if mine is None:
            mine = next((p for p in prior if not p["eid"]
                         and _containment(ids, p["aids"]) >= 0.5), None)
        if mine:
            merged = sorted(set(ids) | mine["aids"])
            if not (set(merged) - mine["aids"]):
                return "skipped"      # nothing new since this was last told
            ids = merged
        arts = [con.execute("SELECT * FROM articles WHERE id=?", (i,)).fetchone()
                for i in ids]
        arts = [a for a in arts if a]
        if not arts:
            return "skipped"
        built = self._build(con, arts, ids, verifier)
        if built is None:
            return "failed"           # LLM unavailable — retried next run
        story, extras = built
        if mine:
            self._update(con, mine["id"], story, extras, event_id)
            mine["aids"] = set(ids)
            mine["eid"] = event_id
            outcome = "updated"
        else:
            sid = self._insert(con, event_id, story, extras)
            prior.append({"id": sid, "eid": event_id, "aids": set(ids)})
            outcome = "new"
        # Per-event commit, for the same reason the general Storyteller
        # commits per group: SQLite has ONE write lock, and holding it
        # across the next event's five LLM calls stalls every signed-in
        # reader (GET /story writes read_stories). It also means an OOM
        # kill mid-run keeps the stories already built.
        con.commit()
        return outcome

    # ---------------------------------------------------------- extraction
    def _build(self, con, arts, ids, verifier):
        """Five LLM calls into one validated FinancialStory. None if any
        essential call failed — a partial finance story is worse than none,
        because the missing part is silently the numbers."""
        texts = fulltext.fetch_for_articles(arts) if len(arts) > 1 else {}
        items, bstats = textmerge.build_brief(arts, texts=texts,
                                              tier_of=verifier.source_tier)
        if not items:
            items = "\n".join(f"- [{a['source']}] {a['title']}: "
                              f"{(a['summary'] or '')[:200]}" for a in arts)
        bstats = dict(bstats or {})
        bstats["kinds"] = verifier.source_breakdown(a["source"] for a in arts)

        verified = verifier.run(con, ids, items)
        if verified is None:
            return None
        claims, verdicts, score, note = verified

        extract = llm.complete_json("fin_extract", prompt("fin_extract", items=items))
        if extract is None:
            return None
        table, metrics, rels = self._tables(extract, arts)

        sentiment = self._sentiment(items, table)

        out = llm.complete_json("fin_story", prompt(
            "fin_story", claims=db.j(claims),
            event_type=_enum(EventType, extract.get("event_type"), EventType.OTHER).value,
            metrics=db.j([m.model_dump(mode="json") for m in metrics]),
            relationships=db.j([{"subject": r.subject, "predicate": r.predicate,
                                 "object": r.object} for r in rels]),
            sentiment=db.j([{"actor": a.actor, "role": a.role.value,
                             "sentiment": a.sentiment} for a in sentiment.actors]),
            items=items,
            depth=depth_hint(bstats, len({a["source"] for a in arts}))))
        if out is None:
            return None

        narrative = out.get("what_happened") or out.get("narrative") or items
        beats = clean_beats(out.get("beats"), narrative)
        anchors = clean_anchors(out.get("anchors"), claims, beats)
        if beats:
            narrative = "\n\n".join(b["text"] for b in beats)
        drivers = [str(d).strip() for d in (out.get("economic_drivers") or [])
                   if str(d).strip()][:4]

        try:
            story = FinancialStory(
                headline=out.get("headline") or arts[0]["title"],
                narrative=narrative,
                event_type=_enum(EventType, extract.get("event_type"), EventType.OTHER),
                economic_drivers=drivers,
                entities=table, metrics=metrics, sentiment=sentiment,
                relationships=rels,
                article_ids=ids,
                sources=sorted({a["source"] for a in arts if a["source"]}),
                sectors=sorted({r.name for r in table.rows
                                if r.type == EntityType.SECTOR}),
                geographies=sorted({r.name for r in table.rows
                                    if r.type == EntityType.GEOGRAPHY}),
                credibility=score, created_at=db.now())
        except ValidationError as e:
            llm._note("invalid_shape", "finance", "fin_story", str(e)[:200])
            return None
        return story, {
            "why_matters": out.get("why_it_matters", ""),
            "claims": {"claims": claims, "verdicts": verdicts},
            "credibility_note": note, "beats": beats, "anchors": anchors,
            "merge_stats": bstats, "image_url": images.best_of(arts),
            "topic": arts[0]["topic"],
        }

    def _tables(self, extract, arts):
        """The tabular CoT output, row by row, each row validated on its own.

        Per-row validation is the whole point: one hallucinated figure is
        dropped and the other eleven survive. Validating the table as a unit
        would make the model's worst row decide the fate of its best.
        """
        aid = arts[0]["id"]
        rows, unresolved = [], []
        for e in (extract.get("entities") or [])[:40]:
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or "").strip()
            if not name:
                continue
            etype = tk.type_of(name, e.get("type"))
            ticker = None
            if etype in (EntityType.ORGANIZATION, EntityType.INDEX):
                resolved = tk.resolve(name)
                if resolved.resolved:
                    ticker = resolved
                else:
                    unresolved.append(name)
            try:
                rows.append(EntityRow(
                    name=name, type=etype, ticker=ticker,
                    role_in_story=str(e.get("role") or "")[:60],
                    verbatim=str(e.get("verbatim") or "")[:400],
                    confidence=0.7 if ticker else 0.5))
            except ValidationError:
                continue
        table = EntityTable(rows=rows, unresolved=sorted(set(unresolved)))

        metrics = []
        for m in (extract.get("metrics") or [])[:30]:
            if not isinstance(m, dict):
                continue
            try:
                metrics.append(Metric(
                    name=str(m.get("name") or "").strip() or "value",
                    value=_num(m.get("value")),
                    unit=_clean(m.get("unit")), currency=_clean(m.get("currency")),
                    period=_clean(m.get("period")),
                    basis=_enum(Basis, m.get("basis"), Basis.UNKNOWN),
                    entity=str(m.get("entity") or "")[:120],
                    yoy_change_pct=_num(m.get("yoy_change")),
                    direction=_direction(_num(m.get("yoy_change"))),
                    verbatim=str(m.get("verbatim") or "").strip()[:400],
                    article_id=aid, confidence=0.6))
            except ValidationError:
                # Almost always the "value but no digit in verbatim" rule —
                # i.e. a number the brief does not contain. Dropping it is the
                # entire reason that rule exists.
                continue

        known = {r.name.casefold() for r in table.rows}
        rels = []
        for r in (extract.get("relationships") or [])[:30]:
            if not isinstance(r, dict):
                continue
            subj, obj = str(r.get("subject") or "").strip(), str(r.get("object") or "").strip()
            # A relationship between things the entity table never established
            # is the model free-associating; both ends must have been extracted.
            if subj.casefold() not in known or obj.casefold() not in known:
                continue
            try:
                rels.append(KGRelationship(
                    subject=subj, object=obj,
                    subject_type=tk.type_of(subj), object_type=tk.type_of(obj),
                    predicate=str(r.get("predicate") or "related_to"),
                    event_type=_enum(EventType, r.get("event_type"), EventType.OTHER),
                    confidence=0.6,
                    evidence=[Evidence(article_id=aid, source=arts[0]["source"],
                                       quote=str(r.get("verbatim") or "")[:600],
                                       url=arts[0]["url"] or "")]))
            except ValidationError:
                continue
        return table, metrics, rels

    def _sentiment(self, items, table):
        """SimTom. Never fatal: a story with no actor read is still a story, so
        a failed call degrades to an empty profile instead of losing the piece."""
        named = [r.name for r in table.rows
                 if r.type in (EntityType.REGULATOR, EntityType.ORGANIZATION,
                               EntityType.PERSON)][:6]
        # Market-side actors are added only when a ticker actually resolved.
        # Without a listed company there is no retail investor or sell-side
        # analyst in the story, and asking for their view would manufacture one.
        if table.tickers():
            named += ["retail investors", "sell-side analysts"]
        if not named:
            return SentimentProfile()
        out = llm.complete_json("fin_sentiment", prompt(
            "fin_sentiment", items=items, actors=", ".join(named)))
        if out is None:
            return SentimentProfile()
        actors = []
        for a in (out.get("actors") or [])[:10]:
            if not isinstance(a, dict):
                continue
            try:
                actors.append(ActorView(
                    actor=str(a.get("actor") or "").strip() or "unnamed",
                    role=_enum(ActorRole, a.get("role"), ActorRole.EXECUTIVE),
                    stated_position=str(a.get("position") or "")[:600],
                    # The two stages of SimTom are kept as written: `knows` is
                    # the perspective-taking step, and storing it is what lets
                    # a reader see WHY an actor reads the event this way.
                    inferred_incentive=" | ".join(
                        x for x in (str(a.get("knows") or "").strip(),
                                    str(a.get("wants") or "").strip()) if x)[:600],
                    sentiment=max(-1.0, min(1.0, _num(a.get("sentiment")) or 0.0)),
                    confidence=max(0.0, min(1.0, _num(a.get("confidence")) or 0.5))))
            except ValidationError:
                continue
        return SentimentProfile(actors=actors,
                                rationale=str(out.get("rationale") or "")[:600])

    # ------------------------------------------------------------ storage
    def _row(self, story: FinancialStory, extras):
        return {
            "headline": story.headline, "narrative": story.narrative,
            "why_matters": extras["why_matters"],
            "event_type": story.event_type.value, "topic": extras["topic"],
            "credibility": story.credibility,
            "credibility_note": extras["credibility_note"],
            "claims": db.j(extras["claims"]),
            "article_ids": db.j(story.article_ids),
            "sources": db.j(story.sources), "sectors": db.j(story.sectors),
            "geographies": db.j(story.geographies), "tickers": db.j(story.tickers),
            "entities": db.j(story.entities.model_dump(mode="json")),
            "metrics": db.j([m.model_dump(mode="json") for m in story.metrics]),
            "sentiment": db.j(story.sentiment.model_dump(mode="json")),
            "sentiment_net": story.sentiment.net,
            "sentiment_dispersion": story.sentiment.dispersion,
            "economic_drivers": db.j(story.economic_drivers),
            "beats": db.j(extras["beats"]) if extras["beats"] else None,
            "anchors": db.j(extras["anchors"]) if extras["anchors"] else None,
            "merge_stats": db.j(extras["merge_stats"]),
            "unresolved": db.j(story.entities.unresolved),
            "image_url": extras["image_url"],
            "schema_version": SCHEMA_VERSION,
        }

    def _insert(self, con, event_id, story, extras):
        sid = db.new_id()
        row = self._row(story, extras)
        row.update({"id": sid, "event_id": event_id,
                    "created_at": db.now(), "updated_at": db.now()})
        cols = ",".join(row)
        con.execute(f"INSERT INTO fin_stories ({cols}) "
                    f"VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        self._write_graph(con, story, sid)
        return sid

    def _update(self, con, sid, story, extras, event_id):
        row = self._row(story, extras)
        row["updated_at"] = db.now()
        # A row matched by article overlap rather than by event_id is a legacy
        # row with no event identity; stamping it here is what lets the next
        # run match it the cheap way instead of overlapping again.
        row["event_id"] = event_id
        sets = ",".join(f"{k}=?" for k in row)
        con.execute(f"UPDATE fin_stories SET {sets} WHERE id=?",
                    tuple(row.values()) + (sid,))
        self._write_graph(con, story, sid)

    def _write_graph(self, con, story: FinancialStory, sid):
        """Entities become nodes even when they take part in no relationship —
        a company that only ever appears alone is still the thing a later story
        connects to, and a node that does not exist cannot be connected."""
        for r in story.entities.rows:
            kg.upsert_node(con, r.name, r.type, r.ticker)
        for rel in story.relationships:
            kg.upsert_edge(con, rel, sid)


def _clean(v):
    s = str(v or "").strip()
    return "" if s.casefold() in ("none", "null", "n/a", "na") else s[:40]


def _direction(pct):
    if pct is None:
        return Direction.UNCLEAR
    return Direction.UP if pct > 0 else (Direction.DOWN if pct < 0 else Direction.FLAT)


# --------------------------------------------------------------- Agent 2
class FinancialTrendAgent:
    """Narrative arcs across a rolling window, with second-order impact taken
    from the knowledge graph rather than from the model.

    The division of labour is deliberate. A language model cannot tell you who
    supplies whom — but the graph Agent 1 built out of the reporting can, and
    it can be walked. So the walk produces the cascade, and the model is asked
    only to explain the mechanism and name the arc.
    """

    NAME_SIM = 0.6        # name cosine above this = the same arc
    STORY_OVERLAP = 0.5   # story containment above this = the same arc

    def run(self, con):
        window_days = config.FIN_TREND_WINDOW_DAYS
        rows = con.execute(
            "SELECT id, headline, event_type, sectors, tickers, metrics, "
            "narrative, credibility FROM fin_stories WHERE updated_at > ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (db.now() - window_days * 86400, config.FIN_MAX_TREND_STORIES)).fetchall()
        retired = con.execute(
            "UPDATE fin_trends SET retired_at=? WHERE retired_at IS NULL "
            "AND updated_at < ?",
            (db.now(), db.now() - window_days * 86400)).rowcount
        if len(rows) < 3:
            con.commit()
            db.log_run(con, "fin_trends", "ok",
                       f"too few finance stories to link ({len(rows)}); "
                       f"retired {retired}")
            return 0

        seeds = self._seeds(con, rows)
        links = kg.cascade(con, seeds)
        existing_names = [r["name"] for r in con.execute(
            "SELECT name FROM fin_trends WHERE retired_at IS NULL "
            "ORDER BY confidence DESC, updated_at DESC LIMIT 30").fetchall()]

        out = llm.complete_json("fin_trend", prompt(
            "fin_trend", n=len(rows), window=int(window_days),
            digests="\n".join(self._digest(r) for r in rows),
            graph=self._graph_text(links),
            existing="\n".join(f"- {n}" for n in existing_names) or "(none yet)"))
        if out is None:
            con.commit()
            db.log_run(con, "fin_trends", "error",
                       f"trend call failed; kept existing set (retired {retired})")
            return 0

        valid = {r["id"] for r in rows}
        fresh = []
        for t in (out.get("trends") or [])[:6]:
            if not isinstance(t, dict):
                continue
            sids = [i for i in (t.get("story_ids") or []) if i in valid]
            if len(sids) < 2:
                continue          # an arc resting on one story is not an arc
            try:
                fresh.append(FinancialTrend(
                    name=str(t.get("name") or "").strip()[:200] or "Financial trend",
                    narrative=str(t.get("narrative") or "")[:4000],
                    arc=[str(a).strip() for a in (t.get("arc") or []) if str(a).strip()][:6],
                    cascade=self._cascade_for(t, links, sids),
                    story_ids=sorted(set(sids)),
                    sectors=_strs(t.get("sectors")), tickers=_strs(t.get("tickers")),
                    macro_factors=_strs(t.get("macro_factors")),
                    window_days=int(window_days),
                    velocity=float(len(sids)),
                    confidence=min(0.85, max(0.0, _num(t.get("confidence")) or 0.4)),
                    created_at=db.now()))
            except ValidationError as e:
                # The commonest rejection here is the advice guard on
                # `narrative`, and an arc that reads as a recommendation is
                # exactly what we do not want stored.
                llm._note("invalid_shape", "finance", "fin_trend", str(e)[:200])
                continue
        if not fresh:
            con.commit()
            db.log_run(con, "fin_trends", "ok",
                       f"no valid arcs this run; kept existing ({retired} retired)")
            return 0

        new_ct, upd_ct = self._reconcile(con, fresh)
        con.commit()
        db.log_run(con, "fin_trends", "ok",
                   f"{new_ct} new + {upd_ct} updated arcs from {len(rows)} stories, "
                   f"{len(links)} graph links, {retired} retired")
        return new_ct + upd_ct

    def _seeds(self, con, rows):
        """Walk seeds: the canonical entities of the window's stories, plus the
        most-mentioned nodes. Bounded — a walk from every node in the graph is
        the unbounded scan this codebase has already been bitten by once."""
        seeds = set()
        for r in rows:
            for t in db.uj(r["tickers"], [])[:4]:
                seeds.add(tk.canonical(t))
        for n in kg.top_entities(con, limit=15):
            seeds.add(n["id"])
        return sorted(s for s in seeds if s)[:25]

    def _digest(self, r):
        nums = []
        for m in db.uj(r["metrics"], [])[:3]:
            if isinstance(m, dict) and m.get("value") is not None:
                nums.append(f"{m.get('name')} {m.get('value')} "
                            f"{m.get('unit') or ''} {m.get('currency') or ''}".strip())
        return (f"{r['id']} | {r['event_type']} | {r['headline']} | "
                f"{', '.join(db.uj(r['sectors'], [])[:3])} | "
                f"{', '.join(db.uj(r['tickers'], [])[:4])} | "
                f"{'; '.join(nums)[:120]} | {(r['narrative'] or '')[:160]}")

    def _graph_text(self, links):
        if not links:
            return "(no relationships established yet)"
        return "\n".join(
            f"- {l.from_entity} -> {l.to_entity} ({l.mechanism}), "
            f"{l.order} hop(s), confidence {l.confidence:.2f}" for l in links[:30])

    def _cascade_for(self, t, links, sids):
        """The trend's cascade: graph links, plus the model's second-order claims.

        If the trend explicitly names entities (via tickers/sectors/second_order),
        prioritize graph links touching those. Otherwise include the strongest
        graph links regardless — an established relationship always speaks louder
        than a model's guess."""
        named = {str(x).casefold() for x in
                 (_strs(t.get("tickers")) + _strs(t.get("sectors")))}
        for so in (t.get("second_order") or []):
            if isinstance(so, dict):
                named.add(str(so.get("entity") or "").casefold())

        # Filter graph links: prioritize those matching named entities, but if
        # nothing matches (named is empty or no overlap), include high-confidence
        # links directly. A silent cascade kills the trend's credibility.
        if named:
            out = [l for l in links
                   if l.from_entity.casefold() in named or l.to_entity.casefold() in named]
        else:
            out = links

        # Append model's second-order claims (lower confidence than stored edges).
        for so in (t.get("second_order") or [])[:6]:
            if not isinstance(so, dict):
                continue
            ent, mech = str(so.get("entity") or "").strip(), str(so.get("mechanism") or "").strip()
            if not ent or not mech:
                continue
            try:
                out.append(ImpactLink(
                    from_entity=(t.get("name") or "trend")[:120], to_entity=ent,
                    mechanism=mech, order=max(2, min(4, int(_num(so.get("order")) or 2))),
                    confidence=0.35))
            except (ValidationError, ValueError):
                continue
        return out[:20]

    def _reconcile(self, con, fresh):
        """Match each fresh arc to a stored one by name or story overlap and
        update in place, so a developing arc keeps its id (and any forecast
        pointing at it) instead of being replaced by a near-duplicate."""
        existing = [{"id": r["id"], "vec": _tf(r["name"] or ""),
                     "sids": db.uj(r["story_ids"], [])}
                    for r in con.execute(
                        "SELECT id, name, story_ids FROM fin_trends").fetchall()]
        new_ct = upd_ct = 0
        for f in fresh:
            vec = _tf(f.name)
            match = next((e for e in existing
                          if cosine(vec, e["vec"]) >= self.NAME_SIM
                          or _containment(f.story_ids, e["sids"]) >= self.STORY_OVERLAP),
                         None)
            payload = {
                "name": f.name, "narrative": f.narrative, "arc": db.j(f.arc),
                "cascade": db.j([c.model_dump(mode="json") for c in f.cascade]),
                "sectors": db.j(f.sectors), "tickers": db.j(f.tickers),
                "macro_factors": db.j(f.macro_factors),
                "window_days": f.window_days, "velocity": f.velocity,
                "confidence": f.confidence,
                "evidence": db.j([e.model_dump(mode="json") for e in f.evidence]),
                "schema_version": SCHEMA_VERSION, "updated_at": db.now(),
            }
            if match:
                sids = sorted(set(match["sids"]) | set(f.story_ids))
                payload["story_ids"] = db.j(sids)
                sets = ",".join(f"{k}=?" for k in payload)
                con.execute(f"UPDATE fin_trends SET {sets}, retired_at=NULL WHERE id=?",
                            tuple(payload.values()) + (match["id"],))
                match["sids"], match["vec"] = sids, vec
                upd_ct += 1
            else:
                tid = db.new_id()
                payload.update({"id": tid, "story_ids": db.j(f.story_ids),
                                "created_at": db.now()})
                cols = ",".join(payload)
                con.execute(f"INSERT INTO fin_trends ({cols}) "
                            f"VALUES ({','.join('?' * len(payload))})",
                            tuple(payload.values()))
                existing.append({"id": tid, "vec": vec, "sids": f.story_ids})
                new_ct += 1
        return new_ct, upd_ct


def _strs(v, limit=8):
    return [str(x).strip() for x in (v or []) if str(x).strip()][:limit]


# --------------------------------------------------------------- Agent 3
class FinancialForecastingAgent:
    """Base / bull / bear scenario analysis per live arc.

    The no-advice rule is enforced twice: the prompt states it, and
    ScenarioForecast rejects prose that breaks it. When the model does break it
    the forecast is not discarded outright — it gets ONE repair call naming the
    exact offending phrase, because the rest of a forecast is usually fine and
    the failure is one sentence.
    """

    TITLE_SIM = 0.6

    def run(self, con):
        retired = con.execute(
            "UPDATE fin_forecasts SET retired_at=? WHERE retired_at IS NULL "
            "AND updated_at < ?",
            (db.now(), db.now() - config.FIN_FORECAST_RETIRE_DAYS * 86400)).rowcount
        trends = con.execute(
            "SELECT * FROM fin_trends WHERE retired_at IS NULL "
            "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (config.FIN_MAX_FORECASTS_PER_RUN,)).fetchall()
        if not trends:
            con.commit()
            db.log_run(con, "fin_forecasts", "ok",
                       f"no live arcs to forecast; retired {retired}")
            return 0
        existing = [{"id": r["id"], "vec": _tf(r["title"] or ""),
                     "tids": db.uj(r["trend_ids"], [])}
                    for r in con.execute(
                        "SELECT id, title, trend_ids FROM fin_forecasts").fetchall()]
        new_ct = upd_ct = failed = repaired = 0
        for t in trends:
            forecast, was_repaired = self._forecast(con, t)
            repaired += 1 if was_repaired else 0
            if forecast is None:
                failed += 1
                continue
            vec = _tf(forecast.title)
            match = next((e for e in existing
                          if cosine(vec, e["vec"]) >= self.TITLE_SIM
                          or t["id"] in e["tids"]), None)
            payload = {
                "title": forecast.title,
                "trend_ids": db.j(sorted(set(forecast.trend_ids) | {t["id"]})),
                "story_ids": db.j(forecast.story_ids),
                "scenarios": db.j([s.model_dump(mode="json")
                                   for s in forecast.scenarios]),
                "short_term": forecast.short_term, "long_term": forecast.long_term,
                "risks": db.j(forecast.risks),
                "dependencies": db.j(forecast.dependencies),
                "invalidation": db.j([i.model_dump(mode="json")
                                      for i in forecast.invalidation]),
                "confidence": forecast.confidence, "disclaimer": forecast.disclaimer,
                "schema_version": SCHEMA_VERSION, "updated_at": db.now(),
            }
            if match:
                sets = ",".join(f"{k}=?" for k in payload)
                con.execute(f"UPDATE fin_forecasts SET {sets}, retired_at=NULL "
                            f"WHERE id=?", tuple(payload.values()) + (match["id"],))
                match["vec"] = vec
                upd_ct += 1
            else:
                fid = db.new_id()
                payload.update({"id": fid, "created_at": db.now()})
                cols = ",".join(payload)
                con.execute(f"INSERT INTO fin_forecasts ({cols}) "
                            f"VALUES ({','.join('?' * len(payload))})",
                            tuple(payload.values()))
                existing.append({"id": fid, "vec": vec, "tids": [t["id"]]})
                new_ct += 1
            con.commit()
        db.log_run(con, "fin_forecasts", "ok",
                   f"{new_ct} new + {upd_ct} updated forecasts over {len(trends)} arcs "
                   f"({failed} failed, {repaired} repaired, {retired} retired)")
        return new_ct + upd_ct

    def _forecast(self, con, trend_row):
        sids = db.uj(trend_row["story_ids"], [])
        digests = []
        for sid in sids[:12]:
            s = con.execute(
                "SELECT id, headline, event_type, narrative FROM fin_stories "
                "WHERE id=?", (sid,)).fetchone()
            if s:
                digests.append(f"{s['id']} | {s['event_type']} | {s['headline']} | "
                               f"{(s['narrative'] or '')[:200]}")
        cascade = db.uj(trend_row["cascade"], [])
        casc_txt = "\n".join(
            f"- {c.get('from_entity')} -> {c.get('to_entity')}: {c.get('mechanism')} "
            f"({c.get('order')} hop)" for c in cascade[:12]) or "(none established)"
        args = dict(
            name=trend_row["name"], arc=" -> ".join(db.uj(trend_row["arc"], [])),
            narrative=(trend_row["narrative"] or "")[:1500],
            sectors=", ".join(db.uj(trend_row["sectors"], [])) or "not stated",
            tickers=", ".join(db.uj(trend_row["tickers"], [])) or "not stated",
            macro=", ".join(db.uj(trend_row["macro_factors"], [])) or "not stated",
            cascade=casc_txt, digests="\n".join(digests) or "(none)")
        out = llm.complete_json("fin_forecast", prompt("fin_forecast", **args))
        if out is None:
            return None, False
        built = self._validate(out, trend_row, sids)
        if built is not None:
            return built, False
        # One repair pass, naming the exact phrase that failed. Worth a second
        # call precisely because the failure is usually one sentence of an
        # otherwise complete analysis.
        offence = self._offence(out)
        if not offence:
            return None, False
        retry = llm.complete_json("fin_forecast", prompt("fin_forecast", **args) +
                                  f"\n\nYour previous answer contained {offence!r}, "
                                  f"which reads as financial advice and was rejected. "
                                  f"Rewrite it as description only: say what may happen "
                                  f"and through what mechanism, name no action for the "
                                  f"reader to take, and give no price or target. "
                                  f"Reply with ONLY valid JSON in the same shape.")
        if retry is None:
            return None, True
        return self._validate(retry, trend_row, sids), True

    def _offence(self, out):
        for text in self._prose(out):
            hit = contains_advice(text)
            if hit:
                return hit
        return None

    @staticmethod
    def _prose(out):
        yield str(out.get("short_term") or "")
        yield str(out.get("long_term") or "")
        for s in (out.get("scenarios") or []):
            if isinstance(s, dict):
                yield str(s.get("thesis") or "")

    def _validate(self, out, trend_row, sids):
        scenarios = []
        for s in (out.get("scenarios") or []):
            if not isinstance(s, dict):
                continue
            try:
                scenarios.append(Scenario(
                    kind=_enum(ScenarioKind, s.get("kind"), ScenarioKind.BASE),
                    probability=max(0.0, min(1.0, _num(s.get("probability")) or 0.0)),
                    thesis=str(s.get("thesis") or "").strip(),
                    horizon=_enum(Horizon, s.get("horizon"), Horizon.SHORT_TERM),
                    direction=_enum(Direction, s.get("direction"), Direction.UNCLEAR),
                    magnitude=str(s.get("magnitude") or "")[:60],
                    affected_sectors=_strs(s.get("affected_sectors")),
                    affected_tickers=_strs(s.get("affected_tickers")),
                    drivers=_strs(s.get("drivers")),
                    precedent=str(s.get("precedent") or "")[:400],
                    confidence=max(0.0, min(1.0, _num(s.get("confidence")) or 0.4))))
            except ValidationError:
                continue
        invalidation = []
        for i in (out.get("invalidation") or []):
            if not isinstance(i, dict):
                continue
            try:
                invalidation.append(Invalidator(
                    observable=str(i.get("observable") or "").strip(),
                    threshold=str(i.get("threshold") or "")[:200],
                    source=str(i.get("source") or "")[:200],
                    by_date=str(i.get("by_date") or "")[:60],
                    invalidates=[_enum(ScenarioKind, k, ScenarioKind.BASE)
                                 for k in (i.get("invalidates") or [])]))
            except ValidationError:
                continue
        try:
            return ScenarioForecast(
                title=str(out.get("title") or trend_row["name"])[:300],
                trend_ids=[trend_row["id"]], story_ids=sids,
                scenarios=scenarios,
                short_term=str(out.get("short_term") or "")[:2000],
                long_term=str(out.get("long_term") or "")[:2000],
                risks=_strs(out.get("risks")), dependencies=_strs(out.get("dependencies")),
                invalidation=invalidation,
                confidence=min(0.85, max(0.0, _num(out.get("confidence")) or 0.4)),
                created_at=db.now())
        except ValidationError as e:
            llm._note("invalid_shape", "finance", "fin_forecast", str(e)[:200])
            return None
