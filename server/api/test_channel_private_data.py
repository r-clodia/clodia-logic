"""Le tre sorgenti che accendono il secondo bit, e nient'altro.

Definizione dell'owner, 17 ago 2026: «un file uploaded oppure un attachment di
email, oppure un collegamento ad un remote». Tutte e tre sono già registrate nei
dati — `provenance` per i file, `remote` nel meta — quindi non si indovina niente.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


class ChannelPrivateDataTests(unittest.TestCase):
    def _con_file(self, alberi: dict, meta: dict | None = None):
        """`alberi`: subpath → voci restituite da `list_files`."""
        with patch.object(channels.topics_client, "list_files",
                          side_effect=lambda t, n, sub="": alberi.get(sub, [])):
            return channels._channel_private_data("SEAL-1", "ops", meta or {})

    def test_only_agent_files_is_false(self) -> None:
        """Working file e scratch: il canale non ha dati riservati."""
        self.assertIs(False, self._con_file({"": [
            {"name": "patch.diff", "kind": "file", "provenance": "agent"},
            {"name": "note.md", "kind": "file", "provenance": "agent"}]}))

    def test_an_upload_from_the_owner_is_true(self) -> None:
        self.assertIs(True, self._con_file({"": [
            {"name": "contratto.pdf", "kind": "file", "provenance": "trusted"}]}))

    def test_an_email_attachment_is_true(self) -> None:
        """Un allegato entra come `untrusted`: non l'ha prodotto un agente."""
        self.assertIs(True, self._con_file({"": [
            {"name": "allegato.xlsx", "kind": "file", "provenance": "untrusted"}]}))

    def test_a_file_without_provenance_counts_as_brought_in(self) -> None:
        """Direzione prudente, la stessa che il taint usa per le etichette
        assenti: un'etichetta mancante non è una garanzia."""
        self.assertIs(True, self._con_file({"": [
            {"name": "misterioso.bin", "kind": "file"}]}))

    def test_a_remote_lights_it_even_with_no_local_files(self) -> None:
        """Dal canale si raggiunge un albero che nessun agente ha prodotto."""
        self.assertIs(True, self._con_file(
            {"": []}, meta={"remote": {"type": "drive", "config": {"folder": "abc"}}}))

    def test_a_vetted_remote_still_lights_it(self) -> None:
        """Il vaglio riguarda l'USCITA (terzo bit), non la presenza dei dati."""
        self.assertIs(True, self._con_file(
            {"": []}, meta={"remote": {"type": "drive", "vetted": True}}))

    def test_it_descends_into_directories(self) -> None:
        alberi = {
            "": [{"name": "local", "kind": "dir"}],
            "local": [{"name": "cliente", "kind": "dir"},
                      {"name": "wip.md", "kind": "file", "provenance": "agent"}],
            "local/cliente": [{"name": "bilancio.pdf", "kind": "file",
                               "provenance": "trusted"}],
        }
        self.assertIs(True, self._con_file(alberi))

    def test_an_empty_channel_is_false(self) -> None:
        self.assertIs(False, self._con_file({"": []}))

    def test_an_unreadable_tree_is_unknown(self) -> None:
        """`None`, non `False`: il punteggio ricade sulla capacità invece di
        rassicurare su dati che potrebbero esserci."""
        with patch.object(channels.topics_client, "list_files",
                          side_effect=RuntimeError("gateway muto")):
            self.assertIsNone(channels._channel_private_data("SEAL-1", "ops", {}))

    def test_a_huge_tree_gives_up_instead_of_guessing(self) -> None:
        """Questo calcolo sta sul percorso di APERTURA di un canale: oltre il
        limite risponde «non lo so» invece di spendere mezzo secondo."""
        alberi = {"": [{"name": f"d{i}", "kind": "dir"} for i in range(100)]}
        for i in range(100):
            alberi[f"d{i}"] = [{"name": f"d{i}b", "kind": "dir"}]
            alberi[f"d{i}/d{i}b"] = [{"name": "x", "kind": "file", "provenance": "agent"}]
        self.assertIsNone(self._con_file(alberi))


if __name__ == "__main__":
    unittest.main()
