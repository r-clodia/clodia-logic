"""Test AgentSpec v2 (refactor logic-only, 12 giu 2026)."""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from .models import AgentSpec


def _minimal_spec(**extra) -> AgentSpec:
    payload = {
        "name": "demo",
        "description": "agente CAP minimale",
        "model": "claude-haiku-4-5",
        "display_name": "Demo",
        "capabilities": ["kanban-operations"],
        "system_prompt": "system-prompt.md",
    }
    payload.update(extra)
    return AgentSpec.model_validate(payload)


class AgentSpecV2Tests(unittest.TestCase):
    def _minimal(self, **extra) -> AgentSpec:
        return _minimal_spec(**extra)

    def test_agent_sdk_defaults_to_claude(self):
        self.assertEqual(self._minimal().agent_sdk, "claude")

    def test_cap_fields_defaults(self):
        spec = self._minimal()
        self.assertEqual(spec.priority, 100)
        self.assertEqual(spec.cost_profile, "standard")
        self.assertEqual(spec.credentials, [])
        self.assertEqual(spec.volumes, [])

    def test_deprecated_fields_still_parse(self):
        # Retrocompatibilità: gli agent.yaml v3 restano validi (warning, non errore)
        spec = self._minimal(skills=["skills/x.md"], can_delegate_to=["other"])
        self.assertEqual(spec.skills, ["skills/x.md"])

    def test_legacy_agent_types_normalize_to_bot(self):

        """agents-notebook A3: esistono solo `bot` e `human` — «non esiste più questa classificazione» per super."""
        self.assertEqual(self._minimal(type="normal").type, "bot")
        self.assertEqual(self._minimal(type="super").type, "bot")
        self.assertEqual(self._minimal(type="bot").type, "bot")

        human = self._minimal(type="human", model=None)
        self.assertEqual(human.type, "human")

    def test_proxy_is_a_registered_non_runtime_principal(self):

        """agents-notebook A11: il proxy è una terza classe — parla e non fa altro."""
        spec = AgentSpec.model_validate({
            "name": "webhook",
            "description": "Sistema terzo ammesso nel topic",
            "display_name": "Webhook",
            "type": "proxy",
            "tool_permissions": ["topic.post_message"],
        })
        self.assertEqual(spec.type, "proxy")
        self.assertIsNone(spec.model)
        self.assertIsNone(spec.system_prompt)
        self.assertIsNone(spec.memory)

    def test_proxy_rejects_runtime_and_extra_tools(self):
        with self.assertRaises(ValidationError):
            AgentSpec.model_validate({
                "name": "webhook",
                "description": "Sistema terzo",
                "display_name": "Webhook",
                "type": "proxy",
                "model": "claude-haiku-4-5",
            })
        with self.assertRaises(ValidationError):
            AgentSpec.model_validate({
                "name": "webhook",
                "description": "Sistema terzo",
                "display_name": "Webhook",
                "type": "proxy",
                "tool_permissions": ["topic.read_file"],
            })
    def test_a_proxy_may_declare_the_four_verbs_it_will_be_given(self):
        """Ciò che il seed dichiara dev'essere ciò che il token conia. Finché
        qui c'era il solo `topic.post_message`, il gateway ne consegnava dieci:
        una restrizione che nessuno applicava, scritta come se lo fosse."""
        spec = AgentSpec.model_validate({
            "name": "crm-esterno",
            "description": "Sistema terzo ammesso nel topic",
            "display_name": "CRM",
            "type": "proxy",
            "tool_permissions": ["topic.post_message", "topic.messages",
                                 "topic.my_mentions", "topic.mark_seen"],
        })
        self.assertEqual(len(spec.tool_permissions), 4)

    def test_the_proxy_surface_stops_at_the_chat(self):
        """Legge il canale, non la stanza: file, ricerca e scrittura restano
        fuori — è la linea che separa un partecipante da una persona."""
        from .models import _PROXY_ALLOWED_TOOLS
        for v in ("topic.read_file", "topic.files", "topic.search", "topic.put",
                  "topic.open", "topic.save_summary", "topic.add_participant"):
            self.assertNotIn(v, _PROXY_ALLOWED_TOOLS)


if __name__ == "__main__":
    unittest.main()


class ActivationMechanicsTests(unittest.TestCase):
    """Il seed dichiara come si attiva: coda, parallelo o rifiuto.

    router-notebook R15: «coda, parallelo e rifiuto sono tutte valide, il profilo
    del seed dovrebbe riportare la sua meccanica di attivazione fra queste tre».

    Il seed dichiarava `routing_mode` (se può essere scelto senza essere
    nominato) e `multi_spawn` (che copre il «parallelo», e solo in parte): «coda»
    e «rifiuto» esistevano come COMPORTAMENTO — il lock FIFO della sessione, il
    cap di `max_spawns`, lo `skip_if_busy` del topic trigger — ma nessuno dei due
    era dichiarato, quindi non si poteva né leggere né scegliere per seed.
    Issue clodia-platform#191.
    """

    def _minimal(self, **extra) -> AgentSpec:
        return _minimal_spec(**extra)

    def test_the_seed_declares_its_activation_mechanics(self) -> None:
        """Il campo esiste: è la riga dove R15 è scritta (decision record 34)."""
        from .models import AgentSpec
        self.assertIn("activation", AgentSpec.model_fields)

    def test_a_seed_that_declares_nothing_queues(self) -> None:
        """Default = il comportamento di oggi, quindi zero cambiamenti in
        produzione: un campo dichiarativo si aggiunge senza migrazione solo se
        il silenzio continua a valere ciò che valeva prima."""
        self.assertEqual(self._minimal().activation, "queue")

    def test_multi_spawn_derives_parallel(self) -> None:
        """`multi_spawn: true` È il «parallelo» di R15: il campo lo legge, non
        chiede di ripeterlo. Due posti da tenere allineati a mano sono un posto
        di troppo."""
        self.assertEqual(self._minimal(multi_spawn=True).activation, "parallel")

    def test_parallel_declared_turns_multi_spawn_on(self) -> None:
        """L'altro verso della stessa derivazione: chi dichiara `parallel`
        ottiene il comportamento, non solo l'etichetta — è la metà-fix che #204
        ha insegnato a non consegnare."""
        spec = self._minimal(activation="parallel")
        self.assertTrue(spec.multi_spawn)

    def test_a_profile_that_contradicts_itself_is_refused(self) -> None:
        """`multi_spawn: true` + `activation: queue` sono due frasi opposte sullo
        stesso seed. Un profilo muto si legge; uno che si contraddice no — e
        l'errore esce in validazione, dove c'è una persona che lo legge."""
        for mech in ("queue", "refuse"):
            with self.subTest(mech), self.assertRaises(ValidationError):
                self._minimal(multi_spawn=True, activation=mech)
        with self.assertRaises(ValidationError):
            self._minimal(multi_spawn=False, activation="parallel")

    def test_refuse_is_declarable(self) -> None:
        spec = self._minimal(activation="refuse")
        self.assertEqual(spec.activation, "refuse")
        self.assertFalse(spec.multi_spawn)

    def test_what_exists_today_covers_only_parallel(self) -> None:
        """La metà che c'è, dichiarata: `multi_spawn` è il «parallelo» di R15.

        Questo test non è un ripiego del precedente — fissa che il pezzo
        esistente resti dichiarativo e leggibile dal seed, così quando arriverà
        `activation` si saprà cosa assorbe.
        """
        from .models import AgentSpec
        self.assertIn("multi_spawn", AgentSpec.model_fields)
        self.assertIn("max_spawns", AgentSpec.model_fields)
        self.assertIn("routing_mode", AgentSpec.model_fields)
