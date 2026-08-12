"""Il preambolo del canale non insegna più `files/`.

`files/` è una forma LEGACY che il gateway accetta ancora in lettura, ma risolve
al backend **effettivo**: su uno scope con un remote Drive punta a Drive, altrove
al locale. Il preambolo la insegnava a ogni turno, e due cose andavano storte.

**Un file caricato prima del mount non si trova più.** Misurato su venere
l'11 ago: `files/8.png` scritto nello store locale alle 14:44:33, mount Drive
creato alle 14:47:21. Da quel momento `_resolve_data_path("files/8.png")` →
`mount=remote/drive`, dove quel file non è mai stato. L'agente che seguiva il
preambolo alla lettera riceveva un file-not-found e concludeva di non avere i
tool.

**E il path riferito a una persona non esiste.** Chi legge «files/x» e apre la
sidebar vede `local/x` o `remote/drive/x`: due nomi per la stessa cosa, di cui
uno solo è quello che si può cercare.

Accettare `files/` resta giusto — i riferimenti già scritti devono continuare a
funzionare — ma un testo iniettato a ogni turno è un maestro, e questo insegnava
la forma ambigua.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


class HintNamesTheRealMountsTests(unittest.TestCase):
    def _hint(self, mounts):
        with patch.object(channels.topics_client, "open_topic",
                          lambda t, n: {"meta": {"mounts": mounts}}):
            return channels._channel_files_hint("SEAL-1", "acme")

    def test_it_does_not_teach_the_legacy_prefix(self):
        testo = self._hint([])
        self.assertNotIn('"files/', testo)
        self.assertNotIn("path \"files/nomefile\"", testo)

    def test_it_says_the_legacy_prefix_must_not_be_used(self):
        self.assertIn("NON usare il prefisso `files/`", self._hint([]))

    def test_a_scope_without_remotes_has_local(self):
        self.assertIn("`local/`", self._hint([]))

    def test_a_mounted_remote_is_named(self):
        """Al PRIMO livello, col suo nome: `comms/`, non `remote/comms/`."""
        testo = self._hint([{"name": "comms", "type": "drive"}])
        self.assertIn("`comms/`", testo)
        self.assertNotIn("remote/comms", testo)

    def test_many_remotes_are_all_named(self):
        testo = self._hint([{"name": "drive", "type": "drive"},
                            {"name": "archivio", "type": "drive"}])
        self.assertIn("`drive/`", testo)
        self.assertIn("`archivio/`", testo)

    def test_a_git_remote_is_not_a_folder(self):
        """Un remote git sono gli STESSI file in un altro momento: annunciarlo
        come cartella produce un path che non si apre (visto il 7 ago)."""
        testo = self._hint([{"name": "repo", "type": "git"}])
        self.assertNotIn("`repo/`", testo)

    def test_it_asks_for_the_path_as_topic_files_returns_it(self):
        """Perché il path serve anche alla persona che legge la risposta."""
        self.assertIn("topic.files", self._hint([]))


class TheGatewayBeingDownDoesNotInventAMountTests(unittest.TestCase):
    def test_local_only_when_the_meta_cannot_be_read(self):
        def boom(t, n):
            raise RuntimeError("gateway irraggiungibile")

        with patch.object(channels.topics_client, "open_topic", boom):
            testo = channels._channel_files_hint("SEAL-1", "acme")
        self.assertIn("`local/`", testo)
        self.assertNotIn("remote/", testo)


if __name__ == "__main__":
    unittest.main()
