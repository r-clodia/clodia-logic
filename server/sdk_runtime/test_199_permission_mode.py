"""`permission_mode` è una decisione DELL'AGENTE, non una riga di tabella.

clodia-platform#199, il residuo di A9. Il resto di A9 è consegnato — i nativi li
dichiara il seed — ma il `permission_mode` era rimasto in `KIND_PERMISSION_MODE`:
`bypassPermissions` per `clodia` e `looper`, scritto accanto al codice. È la
stessa forma di difetto su cui A9 è stata scritta, una riga più in basso.

E era un grado peggio di come la issue lo descrive. Non era un campo letto da
nessuno: `AgentSpec` ha `extra="forbid"`, quindi un `permission_mode:` in un
`agent.yaml` **non caricava affatto** — il seed andava in errore. Non «inerte»:
vietato. Un seed non aveva modo di pronunciarsi nemmeno sbagliando.

## La direzione, e perché non può scalare

La precedenza è **seed → tabella → default dinamico**, cioè l'inverso di
`_resolve_model` (che guarda la tabella per prima). Non è un'incoerenza: per il
modello la tabella È l'autorità dei tre kind statici, qui l'autorità è l'agente.

Il ripiego resta `bypassPermissions`, che è il valore più LARGO dei quattro.
Conseguenza da tenere a mente leggendo questi test: una dichiarazione nel seed
può solo **stringere**. Un agente che riscrivesse il proprio seed non guadagna
niente scrivendoci `bypassPermissions` — ce l'ha già per assenza.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml
from pydantic import ValidationError

from ..agents.loader import registry
from ..agents.models import AgentSpec
from ..config import workspace_path
from . import session as S


def _spec(nome: str, **extra) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": nome,
        "description": "d",
        "display_name": nome.title(),
        "model": "claude-sonnet-4-6",
        "agent_sdk": "claude",
        "system_prompt": "system-prompt.md",
        **extra,
    })


class _ConRegistry(unittest.TestCase):
    """Inietta seed fittizi nel registry e li ritira.

    I seed si costruiscono in `setUp` e non nel corpo della classe: finché il
    campo non esiste, `AgentSpec` solleva, e costruirli all'import farebbe
    fallire la RACCOLTA — un file di test che non parte dice «errore», non dice
    quale controllo è rosso.
    """

    def _seeds(self) -> dict:
        return {}

    def setUp(self) -> None:
        self._saved = dict(registry._agents)
        registry._agents.update(self._seeds())

    def tearDown(self) -> None:
        registry._agents = self._saved


class TheSeedDecidesTests(_ConRegistry):
    def _seeds(self) -> dict:
        return {
            "pianificatore": _spec("pianificatore", permission_mode="plan"),
            "prudente": _spec("prudente", permission_mode="default"),
            "muto": _spec("muto"),
        }

    def test_a_seed_that_declares_it_decides(self) -> None:
        """Prima: `bypassPermissions`, perché la dichiarazione non arrivava."""
        self.assertEqual(S._resolve_permission_mode("pianificatore"), "plan")
        self.assertEqual(S._resolve_permission_mode("prudente"), "default")

    def test_a_seed_that_says_nothing_keeps_the_fallback(self) -> None:
        """`None` = «non mi pronuncio», e non deve STRINGERE: un seed non
        aggiornato non può ritrovarsi con dei prompt che nessuno approva."""
        self.assertEqual(S._resolve_permission_mode("muto"), "bypassPermissions")

    def test_an_unknown_kind_keeps_the_fallback(self) -> None:
        self.assertEqual(S._resolve_permission_mode("inesistente"),
                         "bypassPermissions")


class TheFieldExistsTests(unittest.TestCase):
    def test_the_schema_knows_it(self) -> None:
        """Prima: `extra_forbidden` — il seed non caricava."""
        self.assertIn("permission_mode", AgentSpec.model_fields)
        self.assertIsNone(_spec("x").permission_mode)

    def test_the_four_sdk_values_are_accepted(self) -> None:
        for v in ("default", "acceptEdits", "plan", "bypassPermissions"):
            with self.subTest(v=v):
                self.assertEqual(_spec("x", permission_mode=v).permission_mode, v)

    def test_anything_else_is_refused_at_load(self) -> None:
        """Un valore che l'SDK non conosce non deve arrivare al subprocess: là
        diventa un errore di avvio, o peggio un'opzione ignorata in silenzio."""
        for v in ("bypass", "BypassPermissions", "yes", ""):
            with self.subTest(v=v):
                with self.assertRaises(ValidationError):
                    _spec("x", permission_mode=v)


class TheTableIsOnlyForSeedlessKindsTests(_ConRegistry):
    """`clodia` ha un seed nel pack: la sua decisione sta lì.

    `ada` e `looper` no — restano in tabella, e non per pigrizia: `ada` vale
    `None` (i prompt interattivi del CLI), che il ripiego dinamico NON produce.
    Togliere lei dalla tabella la aprirebbe a `bypassPermissions` senza che
    nessuno l'abbia deciso.
    """

    def _seeds(self) -> dict:
        return {"clodia": _spec("clodia", permission_mode="bypassPermissions")}

    def test_clodia_left_the_table(self) -> None:
        self.assertNotIn("clodia", S.KIND_PERMISSION_MODE)

    def test_and_her_seed_carries_the_same_decision(self) -> None:
        self.assertEqual(S._resolve_permission_mode("clodia"),
                         "bypassPermissions")

    def test_the_seedless_static_kinds_stay(self) -> None:
        self.assertIsNone(S._resolve_permission_mode("ada"))
        self.assertEqual(S._resolve_permission_mode("looper"),
                         "bypassPermissions")


class ThePackSeedDeclaresItTests(unittest.TestCase):
    """La contro-prova sul file: senza questa riga nel pack, la tabella l'ha
    perso e nessuno l'ha ripreso — clodia cadrebbe sul ripiego per caso invece
    che sulla propria dichiarazione. Stesso valore, ma non più scritto da
    nessuna parte."""

    def test_the_pack_seed_of_clodia_declares_bypass(self) -> None:
        f = Path(workspace_path("catalogs/packs/base-pack/agents/clodia/agent.yaml"))
        raw = yaml.safe_load(f.read_text()) or {}
        self.assertEqual(raw.get("permission_mode"), "bypassPermissions")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
