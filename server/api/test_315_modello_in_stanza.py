"""Il MODELLO in uso in questa stanza, accanto al provider (clodia-platform#315).

Follow-up di #310, che ha portato in chat il provider EFFETTIVO per stanza. Il
modello restava fuori, pur essendo GIÀ calcolato: `topic_runtime_override`
sceglie il provider min-cost idoneo al tier **e** il modello abbinato a quel
provider, e mette entrambi nel dict di ritorno. `_topic_provider` leggeva solo
`provider` e buttava via `model`.

Perché provider e modello vanno letti dalla STESSA risposta: non sono campi
indipendenti. `provider_models` abbina un modello a ciascun provider e
`candidate_providers` filtra i provider proprio in base al modello che
servirebbero. Il modello da mostrare in una stanza è quindi quello del provider
scelto per QUEL tier — mostrare il modello di uno stack diverso da quello in uso
sarebbe peggio che non mostrarlo (è il difetto che la card agente ha ancora su
`effective_model`, lavoro separato).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels
from ..sdk_runtime.session import ProviderNotConnected


class _Spec:
    type = "bot"

    def __init__(self, name: str = "avvocato"):
        self.name = name
        self.clearance = "SEAL-2"


class _Umano:
    type = "human"
    name = "davide"
    clearance = "SEAL-4"


def _override(**kw):
    """Sostituisce `topic_runtime_override` come lo vede `channels`."""
    return patch.object(channels, "topic_runtime_override", **kw)


class IlModelloDellaStanza(unittest.TestCase):

    def test_topic_provider_model_legge_il_modello_dello_stesso_override(self) -> None:
        """IL CASO: la stanza SEAL-2 gira su aws-region-eu, e il modello da dire
        è quello abbinato a QUEL provider, non il preferito fuori-stanza."""
        with _override(return_value={"provider": "aws-region-eu",
                                     "model": "eu.anthropic.claude-opus-4-6-v1"}):
            self.assertEqual("eu.anthropic.claude-opus-4-6-v1",
                             channels._topic_provider_model(_Spec(), "SEAL-2"))
            self.assertEqual("aws-region-eu",
                             channels._topic_provider(_Spec(), "SEAL-2"))

    def test_provider_non_connesso_nessun_modello(self) -> None:
        """Stesso trattamento del provider: nessuno stack utilizzabile per il
        tier → non si inventa un modello, si tace."""
        with _override(side_effect=ProviderNotConnected("avvocato", "nessun provider")):
            self.assertIsNone(channels._topic_provider_model(_Spec(), "SEAL-4"))

    def test_errore_inatteso_non_propaga(self) -> None:
        """Un chip della UI non può far fallire l'endpoint di idoneità."""
        with _override(side_effect=RuntimeError("catalogo rotto")):
            self.assertIsNone(channels._topic_provider_model(_Spec(), "SEAL-1"))

    def test_override_senza_modello(self) -> None:
        """`topic_runtime_override` mette `model` solo se lo risolve."""
        with _override(return_value={"provider": "scaleway"}):
            self.assertIsNone(channels._topic_provider_model(_Spec(), "SEAL-1"))


class LaUIRiceveIlModello(unittest.TestCase):
    """`_eligibility` è ciò che la webui legge: se il campo non c'è lì, il chip
    non può esistere."""

    def test_eligibility_porta_provider_e_modello(self) -> None:
        with _override(return_value={"provider": "anthropic-api",
                                     "model": "claude-sonnet-4-5"}), \
                patch.object(channels, "_provider_seal_ok", return_value=True):
            e = channels._eligibility(_Spec(), "SEAL-1")
        self.assertEqual("anthropic-api", e["provider"])
        self.assertEqual("claude-sonnet-4-5", e["model"])

    def test_umano_nessun_modello(self) -> None:
        """Gli umani non trattano dati via provider: né provider né modello."""
        e = channels._eligibility(_Umano(), "SEAL-1")
        self.assertTrue(e["eligible"])
        self.assertIsNone(e["provider"])
        self.assertIsNone(e["model"], "la chiave deve esserci comunque: la UI "
                                      "legge sempre lo stesso oggetto")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
