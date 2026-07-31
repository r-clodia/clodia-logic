from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import channel_aliases


class ChannelAliasesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root_patch = patch.object(channel_aliases, "_ROOT", Path(self.tmp.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_namespaces_are_isolated(self) -> None:
        (Path(self.tmp.name) / "alice").mkdir()
        (Path(self.tmp.name) / "alice" / "channel-aliases.json").write_text(
            json.dumps({"save": "salva"}), encoding="utf-8"
        )
        self.assertEqual(channel_aliases._read("alice"), {"save": "salva"})
        self.assertEqual(channel_aliases._read("bob"), {})

    def test_alias_grammar_preserves_environment_variables(self) -> None:
        self.assertEqual(channel_aliases._normalize({"save": "salva"}), {"save": "salva"})
        with self.assertRaises(Exception):
            channel_aliases._normalize({"PATH": "non valido"})


if __name__ == "__main__":
    unittest.main()
