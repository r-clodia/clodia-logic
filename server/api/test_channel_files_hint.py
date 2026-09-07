"""Il preambolo del canale non insegna più `files/`, né un mount che non c'è più.

Dalla voce 40 (decision-record) l'albero dati di uno scope è sempre e solo
`local/`: il mount Drive/git navigabile è stato ritirato, quindi il preambolo
non ha più bisogno di interrogare il gateway per sapere quali mount nominare.
"""
from __future__ import annotations

import unittest

from . import channels


class HintIsAlwaysLocalTests(unittest.TestCase):
    def _hint(self):
        return channels._channel_files_hint("SEAL-1", "acme")

    def test_it_does_not_teach_the_legacy_prefix(self):
        testo = self._hint()
        self.assertNotIn('"files/', testo)

    def test_it_says_the_legacy_prefix_must_not_be_used(self):
        self.assertIn("NON usare il prefisso `files/`", self._hint())

    def test_it_names_local(self):
        self.assertIn("local/", self._hint())

    def test_it_does_not_mention_a_remote_mount(self):
        self.assertNotIn("remote/", self._hint())

    def test_it_asks_for_the_path_as_topic_files_returns_it(self):
        """Perché il path serve anche alla persona che legge la risposta."""
        self.assertIn("topic.files", self._hint())


if __name__ == "__main__":
    unittest.main()
