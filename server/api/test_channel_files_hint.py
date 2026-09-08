"""Il preambolo del canale non insegna più `files/`, né un mount che non c'è più
— e adesso dice anche QUALI file ci sono già.

Dalla voce 40 (decision-record) l'albero dati di uno scope è sempre e solo
`local/`: il mount Drive/git navigabile è stato ritirato, quindi il preambolo
non ha più bisogno di interrogare il gateway per sapere quali mount nominare.

L'elenco file esiste perché il preambolo diceva solo COME cercare
(`topic.files`), mai COSA c'è: un agente che non pensa a chiamare `topic.files`
da sé risponde senza sapere che il canale ha già la risposta al suo dubbio.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


def _albero(mappa: dict) -> callable:
    """`mappa`: subpath → voci restituite da `list_files`."""
    return lambda tier, name, subpath="": mappa.get(subpath, [])


class HintIsAlwaysLocalTests(unittest.TestCase):
    def _hint(self, mappa=None):
        with patch.object(channels.topics_client, "list_files",
                          _albero(mappa if mappa is not None else {"": []})):
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


class TheListingNamesWhatIsAlreadyThereTests(unittest.TestCase):
    """Il caso della richiesta di Davide: i bot spesso non sanno che un
    documento nel canale risponde già al loro dubbio, perché il preambolo dice
    solo come cercare — mai cosa c'è."""

    def _hint(self, mappa):
        with patch.object(channels.topics_client, "list_files", _albero(mappa)):
            return channels._channel_files_hint("SEAL-1", "acme")

    def test_an_empty_scope_has_no_listing(self):
        testo = self._hint({"": []})
        self.assertNotIn("GIÀ presenti", testo)

    def test_files_are_named_with_their_full_path(self):
        testo = self._hint({
            "": [{"name": "local", "kind": "dir"}],
            "local": [{"name": "summary.md", "kind": "file"}],
        })
        self.assertIn("local/summary.md", testo)
        self.assertIn("GIÀ presenti", testo)

    def test_it_descends_into_directories(self):
        testo = self._hint({
            "": [{"name": "local", "kind": "dir"}],
            "local": [{"name": "contratti", "kind": "dir"},
                      {"name": "note.md", "kind": "file"}],
            "local/contratti": [{"name": "bozza.pdf", "kind": "file"}],
        })
        self.assertIn("local/note.md", testo)
        self.assertIn("local/contratti/bozza.pdf", testo)

    def test_agent_written_files_are_named_too(self):
        """A differenza del secondo bit del trifecta, qui non si filtra per
        provenienza: un file scritto da un agente in una sessione precedente
        risponde a un dubbio tanto quanto uno caricato da una persona."""
        testo = self._hint({"": [
            {"name": "ricerca.md", "kind": "file", "provenance": "agent"},
        ]})
        self.assertIn("ricerca.md", testo)

    def test_an_unreachable_gateway_falls_back_to_the_base_hint(self):
        with patch.object(channels.topics_client, "list_files",
                          side_effect=RuntimeError("gateway muto")):
            testo = channels._channel_files_hint("SEAL-1", "acme")
        self.assertNotIn("GIÀ presenti", testo)
        self.assertIn("topic.files", testo)

    def test_a_tree_too_large_to_fit_the_cap_shows_nothing_rather_than_half(self):
        """Un elenco a metà si legge come completo: meglio nessun elenco che
        uno parziale spacciato per intero."""
        tanti = [{"name": f"f{i}.md", "kind": "file"}
                 for i in range(channels._FILES_HINT_MAX_ENTRIES + 5)]
        testo = self._hint({"": tanti})
        self.assertNotIn("GIÀ presenti", testo)

    def test_the_listing_is_sorted(self):
        testo = self._hint({"": [
            {"name": "zeta.md", "kind": "file"},
            {"name": "alfa.md", "kind": "file"},
        ]})
        self.assertLess(testo.index("alfa.md"), testo.index("zeta.md"))


if __name__ == "__main__":
    unittest.main()
