"""The published API contract.

app/schemas/api.py documents response shapes WITHOUT enforcing them (see that
module for why `response_model` is unsafe here). Documentation that is never
checked drifts, and a third party generating a client from openapi.json has no
way to notice. These tests are the check:

  * every documented endpoint resolves to a real schema, not a bare {}
  * a real response validates against the model documented for it
  * a real response carries no top-level key the model failed to declare
    (the models are extra="allow", so validation alone would not catch this —
     an undocumented field is invisible to a code-generated client)

Rows are seeded through PRAGMA table_info rather than hard-coded INSERTs, so a
schema change does not silently turn these into no-ops.

    cd backend && python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("LLM_PROVIDER", "mock")

from fastapi.testclient import TestClient                           # noqa: E402

from app import config, db                                          # noqa: E402
from app.schemas import api as wire                                 # noqa: E402


def _insert(con, table, values):
    """INSERT only the columns that actually exist, so a dropped or renamed
    column fails loudly here instead of being silently ignored."""
    cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    unknown = set(values) - cols
    assert not unknown, f"{table} has no column(s) {sorted(unknown)} — schema moved"
    use = {k: v for k, v in values.items() if k in cols}
    con.execute(f"INSERT INTO {table} ({','.join(use)}) "
                f"VALUES ({','.join('?' * len(use))})", list(use.values()))


class ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        # config read DB_PATH at import, so set both — prod points it at
        # /var/data, which does not exist here.
        os.environ["DB_PATH"] = config.DB_PATH = os.path.join(cls.tmp, "contract.db")
        # Schema creation is once-per-process. Another test module connecting
        # first would otherwise leave this database empty.
        db._schema_ready = False
        from app import main
        cls.main = main
        cls.client = TestClient(main.app)
        cls.spec = main.app.openapi()
        cls._seed()

    @classmethod
    def _seed(cls):
        con = db.connect()
        now = db.now()
        _insert(con, "articles", {
            "id": "art1", "title": "Bank raises rates", "url": "https://x.test/1",
            "source": "reuters.com", "topic": "business", "fetched_at": now})
        _insert(con, "stories", {
            "id": "news1", "headline": "Bank raises rates",
            "narrative": "A bank raised rates.", "credibility": 40.0,
            "credibility_note": "Two outlets.", "topic": "business",
            "claims": json.dumps({"claims": ["Rates rose."], "verdicts": [
                {"claim": "Rates rose.", "verdict": "corroborated", "note": "ok"}]}),
            "article_ids": json.dumps(["art1"]), "trend_ids": "[]",
            "connection_ids": "[]", "created_at": now, "image_url": ""})
        _insert(con, "fin_stories", {
            "id": "fin1", "event_id": "evt1", "headline": "Lender posts Q1 beat",
            "narrative": "A lender beat estimates.", "why_matters": "Margins held.",
            "credibility": 30.0, "credibility_note": "One outlet.",
            "topic": "finance", "event_type": "earnings",
            "claims": json.dumps({"claims": [], "verdicts": []}),
            "article_ids": json.dumps(["art1"]),
            "sources": json.dumps(["reuters.com"]),
            "sectors": json.dumps(["banking"]), "tickers": json.dumps(["HDFCBANK"]),
            "geographies": "[]",
            "entities": json.dumps({"rows": [], "unresolved": ["Some Startup"]}),
            "metrics": json.dumps([{"name": "eps", "value": 12.0, "unit": "",
                                    "verbatim": "EPS of 12", "basis": "reported",
                                    "direction": "up", "confidence": 0.8}]),
            "sentiment": json.dumps({"actors": [], "rationale": ""}),
            "sentiment_net": 0.2, "sentiment_dispersion": 0.1,
            "economic_drivers": json.dumps(["Rate cycle"]),
            "beats": json.dumps([{"label": "The beat", "text": "It beat."}]),
            "anchors": json.dumps([{"claim": 0, "quote": "It beat."}]),
            "merge_stats": json.dumps({"sources": 1, "conflicts": 0,
                                       "kinds": {"newsroom": 1}}),
            "unresolved": json.dumps(["Some Startup"]), "schema_version": 1,
            "image_url": "", "created_at": now, "updated_at": now})
        _insert(con, "fin_trends", {
            "id": "trend1", "name": "Margin compression",
            "narrative": "Lenders are squeezed.", "arc": json.dumps(["onset"]),
            "cascade": "[]", "story_ids": json.dumps(["fin1"]),
            "sectors": json.dumps(["banking"]), "tickers": "[]",
            "macro_factors": json.dumps(["Policy rate"]), "window_days": 7,
            "velocity": 1.0, "confidence": 0.6, "evidence": "[]",
            "created_at": now, "updated_at": now})
        _insert(con, "fin_forecasts", {
            "id": "fc1", "title": "Rate path", "trend_ids": json.dumps(["trend1"]),
            "story_ids": json.dumps(["fin1"]),
            "scenarios": json.dumps([
                {"kind": k, "probability": p, "thesis": "A thesis.",
                 "horizon": "short_term", "direction": "unclear", "confidence": 0.5}
                for k, p in (("base", 0.5), ("bull", 0.25), ("bear", 0.25))]),
            "short_term": "Flat.", "long_term": "Unclear.",
            "risks": json.dumps(["Policy shift"]),
            "dependencies": json.dumps(["Inflation prints"]),
            "invalidation": json.dumps([{"observable": "CPI", "threshold": ">6%",
                                         "invalidates": ["base"]}]),
            "confidence": 0.5, "disclaimer": "Not investment advice.",
            "created_at": now, "updated_at": now})
        _insert(con, "fin_kg_nodes", {
            "namespace": "finance", "id": "HDFCBANK", "name": "HDFC Bank",
            "type": "organization", "ticker": "HDFCBANK", "exchange": "NSE",
            "mentions": 3, "first_seen": now, "last_seen": now})
        _insert(con, "fin_kg_edges", {
            "id": "e1", "namespace": "finance", "subject": "RBI",
            "predicate": "regulates", "object": "HDFCBANK",
            "subject_type": "organization", "object_type": "organization",
            "event_type": "regulation", "confidence": 0.7, "evidence": "[]",
            "story_ids": json.dumps(["fin1"]), "created_at": now, "updated_at": now})
        _insert(con, "users", {"id": "u1", "created_at": now, "token": "tok1",
                               "context": json.dumps({"interests": ["finance"]})})
        con.commit()
        con.close()

    # ------------------------------------------------------------------ spec
    def test_every_documented_endpoint_has_a_real_schema(self):
        """The regression this whole module exists for: a 200 documented as {}
        tells a client generator nothing, which is what the consuming team hit."""
        want = ["/feed", "/story/{story_id}", "/finance/stories",
                "/finance/story/{story_id}", "/finance/trends",
                "/finance/forecasts", "/finance/graph",
                "/finance/graph/{entity_name}/stories",
                "/finance/causal/chains", "/finance/causal/simulate"]
        for path in want:
            with self.subTest(path=path):
                op = self.spec["paths"][path]["get"]
                schema = op["responses"]["200"]["content"]["application/json"]["schema"]
                self.assertIn("$ref", schema, f"{path} still documents a bare 200")
                self.assertTrue(op.get("tags"), f"{path} is untagged")
                self.assertTrue(op.get("summary"), f"{path} has no summary")

    def test_spec_declares_a_production_server(self):
        """Consumers were fetching openapi.json from the portal host, which
        answers every unknown path with the SPA. An explicit server block is
        what points a generated client at the API host instead."""
        self.assertEqual(self.spec["servers"][0]["url"],
                         "https://newslens-rmv6.onrender.com")

    # ------------------------------------------------------- shape agreement
    AUTH = {"Authorization": "Bearer tok1"}

    def _check(self, path, model, *, allow_missing=()):
        r = self.client.get(path, headers=self.AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        model.model_validate(body)          # documented types must accept it
        declared = set(model.model_fields) | {
            f.alias for f in model.model_fields.values() if f.alias}
        undocumented = set(body) - declared - set(allow_missing)
        self.assertFalse(undocumented,
                         f"{path} sends undocumented key(s) {sorted(undocumented)} — "
                         f"add them to schemas/api.py or a generated client drops them")
        return body

    def test_finance_stories(self):
        body = self._check("/finance/stories?limit=5", wire.FinanceStoryList)
        item = body["stories"][0]
        wire.FinanceStorySummary.model_validate(item)
        self.assertFalse(set(item) - set(wire.FinanceStorySummary.model_fields))

    def test_finance_story_detail(self):
        body = self._check("/finance/story/fin1", wire.FinanceStoryDetail)
        self.assertEqual(body["entities"]["unresolved"], ["Some Startup"])

    def test_finance_trends(self):
        body = self._check("/finance/trends", wire.FinanceTrendList)
        self.assertFalse(set(body["trends"][0]) - set(wire.FinanceTrendOut.model_fields))

    def test_finance_forecasts(self):
        body = self._check("/finance/forecasts", wire.FinanceForecastList)
        fc = body["forecasts"][0]
        self.assertFalse(set(fc) - set(wire.FinanceForecastOut.model_fields))
        self.assertTrue(fc["disclaimer"], "the not-advice framing must be on the wire")

    def test_finance_graph_both_shapes(self):
        self._check("/finance/graph", wire.FinanceGraphResponse)
        self._check("/finance/graph?entity=HDFCBANK", wire.FinanceGraphResponse)

    def test_finance_entity_stories(self):
        self._check("/finance/graph/HDFCBANK/stories", wire.FinanceEntityStories)

    def test_causal_chains_and_simulation(self):
        body = self._check("/finance/causal/chains", wire.CausalChainList)
        chain = body["chains"][0]
        self.assertFalse(set(chain) - set(wire.CausalChain.model_fields))
        sim = self._check(f"/finance/causal/simulate?shock_id={chain['id']}&intensity=25",
                          wire.CausalSimulation)
        self.assertEqual(sim["shock_id"], chain["id"])

    def test_feed_including_a_finance_item(self):
        body = self._check("/feed?user_id=u1", wire.FeedResponse)
        kinds = {it.get("kind") for it in body["items"]}
        self.assertIn("finance", kinds, "finance stories should merge into the feed")
        for it in body["items"]:
            undocumented = set(it) - set(wire.FeedItem.model_fields)
            self.assertFalse(undocumented, f"feed item has undocumented {undocumented}")
        fin = next(it for it in body["items"] if it.get("kind") == "finance")
        self.assertEqual(fin["topic"], "finance",
                         "the topic chips filter on this — it is what puts a "
                         "finance story under the Finance chip")
        self.assertIn("metric_count", fin)

    def test_story_detail_serves_both_pipelines(self):
        news = self._check("/story/news1", wire.StoryDetail)
        self.assertIsNone(news.get("kind"))
        fin = self._check("/story/fin1", wire.StoryDetail)
        self.assertEqual(fin["kind"], "finance")
        self.assertIn("metrics", fin)

    def test_absent_keys_stay_absent(self):
        """The reason these models document rather than enforce. `correction`
        and the claims_* counts are omitted when unknown, and that absence is
        load-bearing: a client must be able to tell "nothing disputed" from
        "never checked". A response_model would have nulled them instead."""
        body = self.client.get("/story/fin1").json()
        self.assertNotIn("correction", body)
        self.assertNotIn("claims_disputed", body)


if __name__ == "__main__":
    unittest.main()
