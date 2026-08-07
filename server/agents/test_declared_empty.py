"""Una lista dichiarata vuota deve arrivare al gateway. Assente e vuoto non sono la stessa cosa.

Come è emerso, il 7 ago 2026. Davide ha notato che spedire una mail gli chiedeva
DUE approvazioni: una sulla destinazione — giusta, l'indirizzo non era in
allowlist — e una sul verbo `email.send`, che secondo il modello non deve esserci
(un gate appartiene all'attraversamento del confine, non al verbo).

Ho tolto `gated_in_channel` dal seed di messaggero: il gate è rimasto. L'ho
dichiarato `[]`: è rimasto ancora. La causa erano due difetti sovrapposti, e
nessuno dei due rompeva niente — il valore semplicemente non arrivava:

1. `AgentSpec` aveva `default_factory=list`, quindi «campo assente» e «campo
   dichiarato vuoto» erano indistinguibili già nel modello;
2. il trasporto scriveva `spec.gated_in_channel or None`, che manda `None` per
   una lista vuota — e `None`, dal lato del gateway, significa di proposito
   «non mi pronuncio, tieni quello che hai».

Il lato ricevente era corretto e costruito apposta per questa distinzione. Era il
lato mittente a non saperla esprimere. Effetto: **rimuovere una voce da un seed
non era possibile** — si toglieva dal file e restava viva nella configurazione.
"""
from __future__ import annotations

import unittest
import inspect

from .models import AgentSpec


BASE = dict(name="prova", description="d", display_name="Prova")


class ModelTests(unittest.TestCase):
    def test_absent_is_none(self):
        """Un seed che non nomina il campo non deve poter azzerare ciò che non
        nomina: è la ragione per cui il gateway è non distruttivo su `None`."""
        s = AgentSpec(**BASE)
        self.assertIsNone(s.gated_in_channel)
        self.assertIsNone(s.gated_tools)
        self.assertIsNone(s.profile_tools)

    def test_declared_empty_is_an_empty_list(self):
        """E un seed che lo dichiara vuoto deve poter azzerare. Se questi due
        casi tornano a coincidere, le rimozioni smettono di funzionare di nuovo
        e nessun test fallisce: è precisamente come è passata inosservata."""
        s = AgentSpec(**BASE, gated_in_channel=[], gated_tools=[], profile_tools=[])
        self.assertEqual(s.gated_in_channel, [])
        self.assertEqual(s.gated_tools, [])
        self.assertEqual(s.profile_tools, [])

    def test_a_declared_list_survives(self):
        s = AgentSpec(**BASE, gated_in_channel=["email.send"])
        self.assertEqual(s.gated_in_channel, ["email.send"])


class TransportTests(unittest.TestCase):
    """Il modello può distinguerli quanto vuole: se il trasporto li riappiattisce
    la distinzione non esiste. Questo test guarda la riga che li appiattiva."""

    def test_the_transport_does_not_collapse_empty_into_none(self):
        from ..api import pack_import
        src = inspect.getsource(pack_import)
        for campo in ("gated_tools", "gated_in_channel", "profile_tools"):
            with self.subTest(campo=campo):
                self.assertNotIn(
                    f"{campo}=spec.{campo} or None", src,
                    f"`or None` su {campo} rimanda a «non mi pronuncio» una lista "
                    "dichiarata vuota: la rimozione non arriva al gateway")

    def test_the_receiving_side_still_treats_none_as_no_opinion(self):
        """L'altra metà del contratto: se il gateway iniziasse ad azzerare su
        `None`, un seed parziale cancellerebbe in silenzio ciò che non nomina —
        il difetto opposto, e più pericoloso."""
        from ..api import gateway_admin
        src = inspect.getsource(gateway_admin.register_agent)
        self.assertIn("is not None", src,
                      "il gateway deve distinguere None da [] anche in ricezione")


if __name__ == "__main__":
    unittest.main()
