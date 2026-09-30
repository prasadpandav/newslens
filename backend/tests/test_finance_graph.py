"""GET /finance/graph must be drawable as sent.

    cd backend && python -m unittest tests.test_finance_graph -v

Nodes are picked by mentions and links by confidence, independently, so a link
could name a node that was never sent. A graph library cannot draw that edge:
the web page threw and fell back to its built-in demo network, so readers saw
invented companies instead of the real graph (60 of 64 links in production).
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_PROVIDER", "mock")

from fastapi.testclient import TestClient                           # noqa: E402

from app import config, db                                          # noqa: E402
from tests.test_openapi import _insert                              # noqa: E402


class FinanceGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = config.DB_PATH = os.path.join(cls.tmp, "graph.db")
        db._schema_ready = False
        from app import main
        cls.client = TestClient(main.app)
        now = time.time()
        old = now - 400 * 86400          # outside any trend window
        con = db.connect()
        for nid, mentions, seen in (("BIG", 50, now), ("SMALL", 1, old), ("OTHER", 2, now)):
            _insert(con, "fin_kg_nodes", {
                "namespace": "finance", "id": nid, "name": nid.title(), "type": "organization",
                "ticker": None, "exchange": "", "mentions": mentions,
                "first_seen": seen, "last_seen": seen})
        _insert(con, "fin_kg_edges", {
            "id": "e1", "namespace": "finance", "subject": "BIG", "predicate": "supplies",
            "object": "SMALL", "subject_type": "", "object_type": "", "confidence": 0.9,
            "evidence": "[]", "story_ids": "[]", "created_at": now, "updated_at": now})
        con.commit()
        con.close()

    def test_every_link_endpoint_is_a_node(self):
        body = self.client.get("/finance/graph?limit=1").json()
        ids = {n["id"] for n in body["top"]}
        self.assertIn("BIG", ids)
        for link in body["links"]:
            self.assertIn(link["from_entity"], ids)
            self.assertIn(link["to_entity"], ids,
                          "a node outside the mentions window is still sent when a link needs it")

    def test_endpoint_nodes_carry_their_real_counts(self):
        body = self.client.get("/finance/graph?limit=1").json()
        small = next(n for n in body["top"] if n["id"] == "SMALL")
        self.assertEqual(small["mentions"], 1)
        self.assertEqual(small["name"], "Small")

    def test_nodes_are_not_duplicated(self):
        body = self.client.get("/finance/graph?limit=5").json()
        ids = [n["id"] for n in body["top"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
