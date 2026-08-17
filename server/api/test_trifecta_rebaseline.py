"""Il reset è una BASELINE: da lì si riparte a misurare, non si smette.

    «il reset del trifecta non significa che smette di funzionare, man mano che
     entrano nuovi dati o ci si collega a ingress non censiti le scimmiette si
     devono riaccendere, il reset approva lo stato corrente come sicuro e da lì
     si riparte a misurare le contaminazioni ed i rischi»
                                                    — Davide, 17 ago 2026

I tre rischi, e il modo in cui ognuno riparte:

1. fonte non censita         → il taint è azzerato al reset e si riaccende al primo
                               ingresso successivo (meccanismo del gateway)
2. dati riservati nel canale → si accende per ciò che NON era nella baseline
3. esfiltrazione su egress   → capacità dei presenti: la baseline è la
                               composizione, cambiarla fa decadere il reset

La prima versione di questo reset azzerava i tre bit e li teneva a zero finché la
composizione non cambiava. Era un silenziamento: un contratto caricato dopo
l'approvazione non avrebbe riaccesso niente.
"""
from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ..agents import trifecta_reset as tr
from . import channels


class NewDataAfterTheBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._env = patch.dict(os.environ, {"CLODIA_DATA": self._tmp.name})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_nothing_new_keeps_the_bit_off(self) -> None:
        voce = tr.set_reset("SEAL-1", "ops", "davide", ["clodia"],
                            data_paths=["local/contratto.pdf#1200"])
        self.assertEqual([], tr.new_private_data(voce, ["local/contratto.pdf#1200"]))

    def test_a_file_added_afterwards_lights_it_again(self) -> None:
        """Il caso della richiesta: un dato che arriva DOPO l'approvazione."""
        voce = tr.set_reset("SEAL-1", "ops", "davide", ["clodia"],
                            data_paths=["local/contratto.pdf#1200"])
        nuovi = tr.new_private_data(voce, ["local/contratto.pdf#1200",
                                           "local/bilancio.xlsx#9000"])
        self.assertEqual(["local/bilancio.xlsx#9000"], nuovi)

    def test_the_same_path_with_new_content_is_new_data(self) -> None:
        """Path + dimensione: un file sostituito allo stesso path è un dato nuovo,
        e senza la dimensione passerebbe per quello approvato."""
        voce = tr.set_reset("SEAL-1", "ops", "davide", ["clodia"],
                            data_paths=["local/nota.md#100"])
        self.assertEqual(["local/nota.md#5000"],
                         tr.new_private_data(voce, ["local/nota.md#5000"]))

    def test_a_remote_connected_afterwards_lights_it(self) -> None:
        """«oppure un collegamento ad un remote»: vale come dato portato dentro,
        anche se al momento del reset non c'era."""
        voce = tr.set_reset("SEAL-1", "ops", "davide", ["clodia"], data_paths=[])
        self.assertEqual(["remote:drive:cartella-x"],
                         tr.new_private_data(voce, ["remote:drive:cartella-x"]))

    def test_removing_data_does_not_light_anything(self) -> None:
        """Togliere non è un rischio nuovo: il bit resta spento."""
        voce = tr.set_reset("SEAL-1", "ops", "davide", ["clodia"],
                            data_paths=["a#1", "b#2"])
        self.assertEqual([], tr.new_private_data(voce, ["a#1"]))


class TheScoreAfterTheBaselineTests(unittest.TestCase):
    """`_dopo_il_reset`: quali bit restano spenti e quali possono riaccendersi."""

    def _prof(self, tainted: int, private: int, egress: int) -> dict:
        return {"bits": {"tainted": tainted, "private_data": private,
                         "arbitrary_egress": egress},
                "score": tainted + private + egress}

    def test_a_new_contamination_survives_the_reset(self) -> None:
        """Il taint è azzerato AL reset; se risulta acceso dopo, è nuovo — e il
        punteggio deve dirlo invece di ereditare l'approvazione."""
        with patch.object(channels, "_private_data_paths", return_value=[]):
            out = channels._dopo_il_reset(
                self._prof(1, 0, 1),
                {"by": "davide", "at": "x", "data_paths": []},
                "SEAL-1", "ops", {})
        self.assertEqual(1, out["bits"]["tainted"])
        self.assertEqual(0, out["bits"]["arbitrary_egress"])
        self.assertEqual(1, out["score"])
        self.assertEqual("1 0 0", out["vector"])

    def test_new_data_after_the_baseline_lights_the_second_bit(self) -> None:
        with patch.object(channels, "_private_data_paths",
                          return_value=["local/nuovo.pdf#10"]):
            out = channels._dopo_il_reset(
                self._prof(0, 1, 1),
                {"by": "davide", "at": "x", "data_paths": ["local/vecchio.pdf#5"]},
                "SEAL-1", "ops", {})
        self.assertEqual(1, out["bits"]["private_data"])
        self.assertEqual(["local/nuovo.pdf#10"], out["new_private_data"])

    def test_an_approved_channel_with_nothing_new_is_zero(self) -> None:
        with patch.object(channels, "_private_data_paths", return_value=["a#1"]):
            out = channels._dopo_il_reset(
                self._prof(0, 1, 1),
                {"by": "davide", "at": "x", "data_paths": ["a#1"]},
                "SEAL-1", "ops", {})
        self.assertEqual(0, out["score"])
        self.assertEqual("0 0 0", out["vector"])

    def test_undeterminable_content_keeps_the_measured_value(self) -> None:
        """Un dubbio non è un'approvazione: se l'albero non si legge, il secondo
        bit resta quello misurato invece di ereditare la baseline."""
        with patch.object(channels, "_private_data_paths", return_value=None):
            out = channels._dopo_il_reset(
                self._prof(0, 1, 0),
                {"by": "davide", "at": "x", "data_paths": []},
                "SEAL-1", "ops", {})
        self.assertEqual(1, out["bits"]["private_data"])

    def test_the_signature_of_who_approved_travels_with_the_score(self) -> None:
        with patch.object(channels, "_private_data_paths", return_value=[]):
            out = channels._dopo_il_reset(
                self._prof(0, 1, 1),
                {"by": "davide", "at": "2026-08-17T10:00:00+00:00", "data_paths": []},
                "SEAL-1", "ops", {})
        self.assertEqual("davide", out["reset_by"])
        self.assertEqual(2, out["score_before_reset"])


if __name__ == "__main__":
    unittest.main()
