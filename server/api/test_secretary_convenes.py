"""Le due decisioni aperte di clodia-platform#196, fissate dove si applicano.

La issue A5 chiedeva di decidere due cose prima di implementare:

1. se la proposta di squadra passi da un **messaggio con marker di invito** (e
   l'owner conferma) oppure vada dritta a `topic.add_participant` in attesa del
   gate. Decisione dell'owner, 6 set 2026: **la prima** — il verbo gated non si
   concede, e la parte di questa decisione che vive nel testo è nel mandato del
   seed (`server/agents/test_seed_mandates.py`);
2. se una stanza avviata dal segretario restituisca il coordinamento a Clodia
   quando il tier cambia, o se il coordinatore sia **fissato alla creazione**.

Il codice implementa già entrambe: `_record_fallback` pesca il coordinatore da
`ai_all` — prima del filtro `state_writer_only` — e `_pending_team_bootstrap` è
one-shot sul marker, con una ri-verifica del provider alla lettura. Questi test
non correggono niente: **bloccano** due comportamenti su cui è stata presa una
decisione, e che oggi nessun test nomina. Sono la forma in cui una decisione
sopravvive a chi l'ha presa: senza, il giorno in cui il ripiego tornasse a pescare
dopo il filtro, il segretario smetterebbe di essere convocabile senza che una
riga lo dica.
"""
from __future__ import annotations

import unittest

from ..agents.models import AgentSpec
from . import channels


def _a(name: str, **extra) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": "bot",
        "clearance": "SEAL-1", "model": "m", "system_prompt": "s.md", **extra,
    })


class SecretaryCoordinatesAboveClodiaSTierTests(unittest.TestCase):
    """A4/A5: il caso che rende il segretario convocabile è il TIER, non l'assenza.

    C'era già un test per «Clodia non è nella stanza». Quello che la issue chiama
    per nome è l'altro: Clodia **è** partecipante, ma il suo provider non copre il
    tier del topic — e allora coordina il segretario, che è `all_tier`.
    """

    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia"),
            "worker": _a("worker"),
            "segretario": _a("segretario", routing_mode="state_writer_only",
                             all_tier=True),
        }
        self._orig_get = channels.registry.get_by_name
        self._orig_ok = channels._provider_seal_ok
        channels.registry.get_by_name = self.agents.get
        # Il provider di Clodia non copre il tier: è la condizione di A4, e si
        # esprime QUI perché è ciò che `_provider_seal_ok` misura davvero — il
        # SEAL del motore che tratterà i dati, non la clearance del seed.
        channels._provider_seal_ok = lambda s, tier: s.name != "clodia"

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._provider_seal_ok = self._orig_ok

    def test_the_secretary_takes_the_coordination_turn(self) -> None:
        trace: dict = {}
        picked = channels._pick_responder(
            ["owner", "worker", "clodia", "segretario"], "SEAL-3", None,
            trace=trace, coordinator_only=True)

        self.assertEqual("segretario", picked.name)
        self.assertEqual("coordinator", trace["mode"])
        self.assertIn("segretario", trace["reason"])

    def test_state_writer_only_does_not_hide_it_from_the_fallback(self) -> None:
        """Il filtro di rilevanza e il ripiego guardano due liste diverse.

        `state_writer_only` toglie il segretario dalla GARA di rilevanza; se lo
        togliesse anche dal ripiego, la ruling dell'11 ago («il coordinatore è
        sempre il segretario a meno che non sia presente clodia») non avrebbe
        nessun modo di scattare, e la stanza cadrebbe sul rango.
        """
        self.assertFalse(
            channels._auto_routing_allowed(self.agents["segretario"],
                                           "che ne pensi del provider?"),
            "il segretario è tornato selezionabile per rilevanza: questo test "
            "non misura più il ripiego")
        trace: dict = {}
        picked = channels._pick_responder(
            ["owner", "worker", "segretario"], "SEAL-3", None,
            trace=trace, coordinator_only=True)

        self.assertEqual("segretario", picked.name)
        self.assertEqual("coordinator", trace["mode"])


class BootstrapDoesNotTransferTests(unittest.TestCase):
    """Seconda decisione: il coordinatore del bootstrap è fissato alla creazione.

    Il marker `<!-- team-bootstrap=nome -->` nomina UN agente. Alzare il tier
    dopo la creazione non riapre la scelta e non passa il turno a Clodia: il
    bootstrap **cade**, e il canale riparte dalla regola ordinaria. È l'esito
    prudente dei due — trasferirlo significherebbe far introdurre la stanza da
    chi non ha visto nascere la richiesta.
    """

    WELCOME = {"kind": "ai", "author": "segretario",
               "text": "Di cosa tratta?\n<!-- team-bootstrap=segretario -->"}

    def setUp(self) -> None:
        self.agents = {"clodia": _a("clodia"),
                       "segretario": _a("segretario", all_tier=True)}
        self._orig_get = channels.registry.get_by_name
        self._orig_ok = channels._provider_seal_ok
        channels.registry.get_by_name = self.agents.get

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._provider_seal_ok = self._orig_ok

    def _pending(self, eligible):
        channels._provider_seal_ok = lambda s, tier: s.name in eligible
        return channels._pending_team_bootstrap(
            [self.WELCOME], ["owner", "clodia", "segretario"], "SEAL-3")

    def test_it_holds_while_the_named_agent_still_fits_the_tier(self) -> None:
        pending = self._pending({"clodia", "segretario"})
        self.assertIsNotNone(pending)
        self.assertEqual("segretario", pending.name)

    def test_a_raised_tier_drops_it_instead_of_handing_it_to_clodia(self) -> None:
        # Clodia è partecipante E idonea: se la scelta si riaprisse, sarebbe lei.
        self.assertIsNone(self._pending({"clodia"}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
