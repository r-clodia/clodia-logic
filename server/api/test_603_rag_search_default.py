"""#603: the responder routing sends the text of every channel message to
rag-search. Without EU_RAG_SEARCH_URL it must reach the container of the
stack, never a host outside it."""
import importlib
import os
import unittest
from unittest.mock import patch

from . import responder_routing


class RagSearchDefaultTest(unittest.TestCase):
    def tearDown(self):
        importlib.reload(responder_routing)

    def test_default_is_the_rag_search_container(self):
        env = {k: v for k, v in os.environ.items() if k != "EU_RAG_SEARCH_URL"}
        with patch.dict(os.environ, env, clear=True):
            mod = importlib.reload(responder_routing)
        self.assertEqual(mod.EMBED_URL, "http://rag-search:7900")

    def test_the_variable_still_wins(self):
        with patch.dict(os.environ, {"EU_RAG_SEARCH_URL": "http://rag.example:7900/"}):
            mod = importlib.reload(responder_routing)
        self.assertEqual(mod.EMBED_URL, "http://rag.example:7900")


if __name__ == "__main__":
    unittest.main()
