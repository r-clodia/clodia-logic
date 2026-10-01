"""Le due rotte che rendono consultabile il ragionamento di un turno finito.

clodia-platform#484. L'indice dice QUALI bolle hanno un ragionamento salvato
(è ciò che accende il 💭 in UI, e senza di lui il bottone aprirebbe il vuoto);
la lettura restituisce il testo di una bolla sola.

La proprietà che conta più del formato: il ragionamento cita il contenuto del
canale, quindi **si legge solo da dentro il canale**. Non è una rotta
amministrativa né una rotta pubblica: sta dietro `_require_member`, la stessa
guardia dei messaggi, e la guardia scatta PRIMA di toccare lo store.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from . import channels


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(channels.router)
    return TestClient(app)


_INDICE = "/clodia/channels/SEAL-2/preventivi-tomato/reasoning"
_UNO = _INDICE + "/m1"
_TOPIC = {"meta": {"tier": "SEAL-2", "participants": ["davide"]}}


class SoloDaDentroIlCanale(unittest.TestCase):
    def test_lindice_e_per_i_partecipanti(self) -> None:
        with patch.object(channels.topics_client, "async_open_topic",
                          new=AsyncMock(return_value=_TOPIC)), \
             patch.object(channels, "_require_member",
                          side_effect=HTTPException(403, "non sei partecipante")), \
             patch.object(channels.reasoning_log, "index") as leggi:
            r = _app().get(_INDICE)
        self.assertEqual(403, r.status_code)
        leggi.assert_not_called()

    def test_la_lettura_e_per_i_partecipanti(self) -> None:
        with patch.object(channels.topics_client, "async_open_topic",
                          new=AsyncMock(return_value=_TOPIC)), \
             patch.object(channels, "_require_member",
                          side_effect=HTTPException(403, "non sei partecipante")), \
             patch.object(channels.reasoning_log, "read") as leggi:
            r = _app().get(_UNO)
        self.assertEqual(403, r.status_code)
        leggi.assert_not_called()

    def test_un_canale_che_non_esiste_e_404(self) -> None:
        with patch.object(channels.topics_client, "async_open_topic",
                          new=AsyncMock(return_value=None)):
            self.assertEqual(404, _app().get(_INDICE).status_code)


class CosaRestituisce(unittest.TestCase):
    def setUp(self) -> None:
        self._p = [
            patch.object(channels.topics_client, "async_open_topic",
                         new=AsyncMock(return_value=_TOPIC)),
            patch.object(channels, "_require_member", return_value="davide"),
        ]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)

    def test_lindice_e_la_lista_dei_messaggi_che_ne_hanno_uno(self) -> None:
        with patch.object(channels.reasoning_log, "index",
                          return_value=["m1", "m7"]):
            r = _app().get(_INDICE)
        self.assertEqual(200, r.status_code)
        self.assertEqual(["m1", "m7"], r.json()["messages"])

    def test_la_lettura_porta_il_testo_e_se_e_troncato(self) -> None:
        voce = {"message_id": "m1", "spawn": "clodia-7", "text": "ci penso",
                "truncated": False, "ts": "2026-10-01T10:00:00+00:00"}
        with patch.object(channels.reasoning_log, "read", return_value=voce):
            r = _app().get(_UNO)
        self.assertEqual(200, r.status_code)
        self.assertEqual("ci penso", r.json()["text"])
        self.assertEqual("clodia-7", r.json()["spawn"])
        self.assertIs(False, r.json()["truncated"])

    def test_una_bolla_senza_ragionamento_e_404(self) -> None:
        """Mai una risposta vuota con 200: in UI diventerebbe un riquadro
        aperto su niente, che è la bolla fantasma che non vogliamo."""
        with patch.object(channels.reasoning_log, "read", return_value=None):
            self.assertEqual(404, _app().get(_UNO).status_code)

    def test_lo_store_viene_interrogato_sul_canale_della_rotta(self) -> None:
        with patch.object(channels.reasoning_log, "index",
                          return_value=[]) as leggi:
            _app().get(_INDICE)
        self.assertEqual(("SEAL-2", "preventivi-tomato"), leggi.call_args.args[:2])


class LeRotteStannoNellApp(unittest.TestCase):
    """Montate davvero sull'app assemblata, non solo nel modulo: una rotta
    dichiarata in un router che nessuno include non esiste (cfr.
    `test_no_hook_surface`, stesso motivo per cui si guarda l'OpenAPI e non
    `app.routes`)."""

    def test_entrambe_le_rotte_sono_servite(self) -> None:
        from .. import main
        paths = main.create_app().openapi()["paths"]
        self.assertIn("/clodia/channels/{tier}/{name}/reasoning", paths)
        self.assertIn("/clodia/channels/{tier}/{name}/reasoning/{message_id}",
                      paths)


if __name__ == "__main__":
    unittest.main()
