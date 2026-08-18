"""Gli avvisi del seed devono USCIRE dal server, non restare nel log.

clodia-platform#227. `_incoerenze` esiste da clodia-logic#303 e scrive in
`LOG.warning`: un rilevatore che parla su un canale che nessuno guarda ha la
stessa forma del difetto che doveva rilevare — «non fallisce, rimuove», e se ne
accorge un umano settimane dopo.

Questi test guardano il PUNTO DI GIUNTURA, che è ciò che può rompersi in
silenzio: la scheda dell'agente e l'elenco possono essere riscritti senza che
nessuno si accorga di aver lasciato indietro un campo, e la pagina tornerebbe
muta esattamente come prima.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from ..agents.loader import registry
from ..agents.models import AgentSpec
from . import agent_registry as AR

AVVISO = ("`allow_shell_cmds` autorizza 2 comandi (git, pytest) ma `native_tools` "
          "non concede `Bash`: comandi che l'agente non ha modo di eseguire")


def _spec(name: str) -> AgentSpec:
    return AgentSpec.model_validate(
        {"name": name, "description": "d", "display_name": name,
         "model": "claude-sonnet-4-5", "system_prompt": "s.md",
         "native_tools": ["Grep"],
         "sandbox": {"allow_shell_cmds": ["git", "pytest"]}})


class WarningsReachTheApiTests(unittest.TestCase):
    def setUp(self) -> None:
        registry._agents["seed_incoerente"] = _spec("seed_incoerente")
        registry._agents["seed_pulito"] = _spec("seed_pulito")
        registry._warnings["seed_incoerente"] = [AVVISO]

    def tearDown(self) -> None:
        registry._agents.pop("seed_incoerente", None)
        registry._agents.pop("seed_pulito", None)
        registry._warnings.pop("seed_incoerente", None)

    def _detail(self, name: str) -> dict:
        with patch.object(AR, "_require_self_or_admin", lambda *_a, **_k: None):
            return asyncio.run(AR.get_agent(name, request=None))  # type: ignore[arg-type]

    def test_the_agent_card_carries_its_warnings(self) -> None:
        d = self._detail("seed_incoerente")
        self.assertEqual([AVVISO], d["warnings"])

    def test_a_coherent_agent_carries_an_empty_list(self) -> None:
        """Lista vuota e campo assente si renderebbero uguale, ma solo il primo
        dice «ho guardato»: la scheda non deve dedurre il silenzio da un buco."""
        self.assertEqual([], self._detail("seed_pulito")["warnings"])

    def test_the_list_endpoint_carries_the_map(self) -> None:
        r = asyncio.run(AR.list_agents())
        self.assertIn("seed_incoerente", r["warnings"])
        self.assertNotIn("seed_pulito", r["warnings"])

    def test_the_list_endpoint_also_marks_each_agent(self) -> None:
        """Per agente, non solo nella mappa in coda: l'elenco può marcare la riga
        senza incrociare due strutture (ed è dove si vede il primo giorno)."""
        r = asyncio.run(AR.list_agents())
        per_nome = {a["name"]: a for a in r["agents"]}
        self.assertEqual([AVVISO], per_nome["seed_incoerente"]["warnings"])
        self.assertEqual([], per_nome["seed_pulito"]["warnings"])

    def test_warnings_are_not_errors(self) -> None:
        """Un seed contraddittorio resta un agente sano agli occhi di `errors`,
        o la webui lo mostrerebbe come non caricato."""
        r = asyncio.run(AR.list_agents())
        self.assertNotIn("seed_incoerente", r["errors"])


if __name__ == "__main__":
    unittest.main()
