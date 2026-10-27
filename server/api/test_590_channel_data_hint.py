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
        channels._CATALOG_CACHE.update(at=float("-inf"), rows={})   # stale on any clock
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
        self.assertIn("eu-normativa", h)               # every participant reads the RAG (#621)

    def test_nothing_granted_nothing_said(self):
        self.assertNotIn("datastore", self.hint("nessuno"))   # a datastore needs its grant
        self.assertEqual(self.hint(None), "")
        self.assertEqual(self.hint("sales", meta={}), "")


class ToolsHintTests(unittest.TestCase):
    """#620/#622: the room's MCPs are named, and deferred tools are explained
    (27 Oct: clodia-744 had email_read and said it did not)."""

    def hint(self, box):
        with patch.object(channels.topics_client, "topic_tools", lambda t, n: box):
            return channels._channel_tools_hint("SEAL-1", "earnext")

    def test_the_rooms_mcps_and_toolsearch(self):
        h = self.hint({"mcp": {"email": {}, "github": {}},
                       "mcp_catalogue": {"email": {"label": "Email", "namespaces": ["email"]},
                                         "github": {"label": "GitHub", "namespaces": ["github"]}}})
        self.assertIn("Email (email.*)", h)
        self.assertIn("GitHub (github.*)", h)
        self.assertIn("ToolSearch", h)

    def test_no_mcp_no_hint_and_a_failure_costs_nothing(self):
        self.assertEqual(self.hint({"mcp": {}}), "")
        def boom(t, n):
            raise channels.topics_client.TopicsClientError("giù")
        with patch.object(channels.topics_client, "topic_tools", boom):
            self.assertEqual(channels._channel_tools_hint("SEAL-1", "x"), "")


if __name__ == "__main__":
    unittest.main()
