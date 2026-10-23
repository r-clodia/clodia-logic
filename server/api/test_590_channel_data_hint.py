"""#590 — the channel preamble tells an agent which imported data it may use
(an agent granted a RAG index looked for it among the files)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels

META = {"index": {"collection": "topic-own"},
        "data": [{"kind": "rag", "name": "topic-own", "grants": {"sales": "read"}},
                 {"kind": "rag", "name": "topic-da98f920", "grants": {"sales": "read", "x": "read"}},
                 {"kind": "datastore", "name": "tomato/leads", "grants": {"sales": "write"}},
                 {"kind": "rag", "name": "eu-normativa", "grants": {"x": "read"}}]}


class DataHintTests(unittest.TestCase):
    def hint(self, agent, meta=META):
        channels._CATALOG_CACHE.update(at=0.0, rows={})
        with patch.object(channels.topics_client, "open_topic", lambda t, n: {"meta": meta}), \
                patch.object(channels.topics_client, "data_catalog", lambda: {"entries": [
                    {"name": "topic-da98f920", "topic": "SEAL-1/corso-confindustria"}]}):
            return channels._channel_data_hint("SEAL-1", "tomato-deck", agent)

    def test_what_the_agent_may_use_and_how(self):
        h = self.hint("sales")
        self.assertIn("indice RAG di QUESTO topic", h)
        self.assertIn("indice RAG del topic corso-confindustria", h)
        self.assertIn('rag.search(collection="topic-da98f920"', h)
        self.assertIn('datastore.read(datastore="tomato/leads"', h)
        self.assertIn("datastore.write", h)
        self.assertNotIn("eu-normativa", h)            # not granted to sales

    def test_nothing_granted_nothing_said(self):
        self.assertEqual(self.hint("nessuno"), "")
        self.assertEqual(self.hint(None), "")
        self.assertEqual(self.hint("sales", meta={}), "")


if __name__ == "__main__":
    unittest.main()
