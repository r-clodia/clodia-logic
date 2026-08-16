"""Test AgentSpec v2 (refactor logic-only, 12 giu 2026)."""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from .models import AgentSpec


class AgentSpecV2Tests(unittest.TestCase):
    def _minimal(self, **extra) -> AgentSpec:
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

    Oggi il seed dichiara `routing_mode` (se può essere scelto senza essere
    nominato) e `multi_spawn` (che copre il «parallelo», e solo in parte): «coda»
    e «rifiuto» esistono come COMPORTAMENTO — il lock FIFO della sessione, il
    cap di `max_spawns` — ma nessuno dei due è dichiarato, quindi non si può né
    leggere né scegliere per seed. Issue clodia-platform#191.
    """

    @unittest.expectedFailure   # clodia-platform#191 — R15 non ancora consegnata
    def test_the_seed_declares_its_activation_mechanics(self) -> None:
        """ROSSO ATTESO finché #191 è aperta.

        Diventerà `unexpected success` il giorno in cui il campo arriva, il che
        è il segnale giusto: cade su questa riga, che è dove il requisito è
        scritto (decision record 34). Senza il test, R15 sarebbe indistinguibile
        da una voce consegnata — che è come A7 è rimasta aperta senza che la sua
        assenza si notasse.
        """
        from .models import AgentSpec
        self.assertIn("activation", AgentSpec.model_fields)

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
