"""La stanza che nessun coordinatore può servire si vede, invece di tacere.

clodia-platform#607: «la clearance effettiva è quella del provider. Per i topic
SEAL-3+ il segretario gira su un provider a quel livello, **oppure il topic
viene segnalato** quando non ce n'è nessuno disponibile».

Il caso non è teorico: i provider Anthropic del catalogo arrivano a SEAL-2 e
l'unico SEAL-3 è Scaleway. Basta metterlo in pausa, o scollegarlo, perché una
stanza confidenziale resti senza nessuno che possa prenderne i turni — e fino a
qui l'unico modo di accorgersene era mandare un messaggio e non vedere arrivare
niente (`_announce_provider_inadeguato` parla quando un turno NON parte, cioè
dopo). Il bit sta nella risposta che la pagina già chiede, perché la webui (R5,
#609) possa dirlo prima.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import channels


def _spec(name: str, **kw):
    base = {"name": name, "type": "bot", "clearance": "SEAL-2",
            "providers": ["claude-team"], "provider": None,
            "agent_sdk": "claude", "model": "claude-sonnet-5-5",
            "provider_models": {}}
    base.update(kw)
    return SimpleNamespace(**base)


AGENTI = {"segretario": _spec("segretario"), "worker": _spec("worker"),
          "davide": _spec("davide", type="human", clearance="SEAL-4")}


class StatoDelCoordinatoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: AGENTI.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig

    def _stato(self, tier: str, servibile: bool, parti=("davide", "segretario", "worker")):
        meta = {"tier": tier, "owner": "davide", "participants": list(parti)}
        with patch.object(channels, "_provider_seal_ok", return_value=servibile):
            return channels._stato_coordinatore(meta, tier)

    def test_coordinatore_servibile_nessuna_segnalazione(self) -> None:
        stato = self._stato("SEAL-3", servibile=True)
        self.assertEqual("segretario", stato["name"])
        self.assertTrue(stato["available"])
        self.assertIsNone(stato["reason"])

    def test_nessun_provider_al_tier_la_stanza_e_segnalata(self) -> None:
        stato = self._stato("SEAL-3", servibile=False)
        self.assertIsNone(stato["name"])
        self.assertFalse(stato["available"])
        self.assertIn("SEAL-3", stato["reason"])
        # la ragione nomina CHI avrebbe dovuto coordinare: senza, chi legge
        # non sa quale provider collegare per rimettere in piedi la stanza.
        self.assertIn("segretario", stato["reason"])

    def test_coordinatore_non_partecipante_e_un_caso_segnalato(self) -> None:
        """Il rango non è un coordinatore: una stanza dove nessun coordinatore
        dichiarato siede resta segnalata anche se qualcuno risponderebbe."""
        stato = self._stato("SEAL-1", servibile=True, parti=("davide", "worker"))
        self.assertIsNone(stato["name"])
        self.assertFalse(stato["available"])

    def test_il_bit_viaggia_con_il_canale(self) -> None:
        """Sta nella risposta che la pagina già chiede: un endpoint a parte
        raddoppierebbe le richieste per un dato che cambia quando cambia la
        composizione della stanza."""
        meta = {"tier": "SEAL-1", "owner": "davide",
                "participants": ["davide", "segretario"]}
        with patch.object(channels.topics_client, "open_topic",
                          return_value={"meta": meta}), \
                patch.object(channels, "_require_member", lambda *a, **k: None), \
                patch.object(channels.access_log, "touch", lambda *a, **k: None), \
                patch.object(channels, "reconcile_membership", lambda *a, **k: []), \
                patch.object(channels, "ensure_auto_participant", lambda *a, **k: None), \
                patch.object(channels, "_live_instances", lambda *a, **k: {}), \
                patch.object(channels, "_ruoli_mostrati", lambda *a, **k: []), \
                patch.object(channels, "_channel_trifecta", lambda *a, **k: None), \
                patch.object(channels, "_provider_seal_ok", return_value=False):
            topic = channels.channel_open("SEAL-1", "ops", object())
        self.assertFalse(topic["coordinator"]["available"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
