"""Un permesso concesso e non registrato al gateway non è concesso (#304).

`PATCH /api/agents/<n>/caps` scriveva `agent.yaml` sulla datadir e si fermava lì.
Ma l'autorizzazione non si legge dalla datadir: `whitelist._agent_may` guarda
`allowed_tools` nella config del GATEWAY, che vive su un volume che questo
processo non monta — per progetto, perché l'autorità dev'essere irraggiungibile
dal suo soggetto (§3.5).

L'effetto misurato: `agents.grant_tool` rispondeva `{"ok": true}` su un verbo che
restava negato a ogni chiamata. Il file mostrava il permesso, l'agente lo vedeva
nella propria lista, e la chiamata falliva con un messaggio che sembrava un
problema di ruolo. L'`avvocato` è passato due giorni da `sysadmin` per aggirare
un grant che risultava applicato e non lo era — cioè il least-privilege è stato
aggirato da un permesso che sembrava esserci.

La revoca è la direzione peggiore: un verbo tolto sulla carta che resta attivo.

Il canale verso il gateway esisteva già ed è quello che usa l'install di un pack
(`gateway_admin.register_agent`): mancava la chiamata in questo punto, e mancarla
qui la fa mancare a TUTTI i percorsi che passano dal PATCH — verbo `agents.*`,
API e pannello insieme.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from fastapi import HTTPException

from . import agent_registry as AR
from . import gateway_admin


#: Lista indentata: era l'UNICA forma che `_set_yaml_list` riconosceva come
#: figlia della chiave (bug chiuso il 13 set 2026, v. `SetYamlListIndentTests`
#: più sotto) — uno stile a indentazione ZERO (`tool_permissions:` seguita da
#: `- x` allo stesso livello della chiave, la forma dominante in questo repo,
#: prodotta anche da `yaml.safe_dump`) non veniva consumato e restava
#: duplicato in coda al file. Qui resta indentata comunque: è la forma che
#: questo test aveva SEMPRE usato, e cambiarla non aggiunge copertura — la
#: aggiunge la classe dedicata sotto.
SEED = """name: avvocato
description: seed di prova
type: normal
clearance: SEAL-1
tool_permissions:
  - "topic.open"
  - "topic.write_file"
"""


class _Spec(SimpleNamespace):
    pass


class _Registry:
    """Registry finta: legge e rilegge l'agent.yaml vero su disco."""

    def __init__(self, agent_dir: Path):
        self.agent_dir = agent_dir
        self.load()

    def load(self):
        d = yaml.safe_load((self.agent_dir / "agent.yaml").read_text()) or {}
        self._spec = _Spec(name=d.get("name"), agent_dir=str(self.agent_dir),
                           type=d.get("type"), immutable=False,
                           tool_permissions=list(d.get("tool_permissions") or []),
                           capabilities=[], rules=[])
        return None

    def get_by_name(self, name):
        return self._spec if name == self._spec.name else None

    def errors(self):
        return {}


class CapsPatchTests(unittest.TestCase):
    def _patch(self, registrazione, **campi):
        """Esegue davvero `patch_agent_caps` su un seed su disco, sostituendo il
        solo lato esterno: la registrazione al gateway."""
        with tempfile.TemporaryDirectory() as tmp:
            adir = Path(tmp) / "avvocato"
            adir.mkdir()
            (adir / "agent.yaml").write_text(SEED, encoding="utf-8")
            reg = _Registry(adir)

            async def _reg_async(*a, **k):
                return registrazione(*a, **k)

            with patch.object(AR, "registry", reg), \
                    patch.object(AR, "_principal_from_request", lambda r: "clodia"), \
                    patch.object(AR, "_agent_can_admin", lambda c: True), \
                    patch.object(gateway_admin, "register_agent_async", _reg_async):
                corpo = AR.AgentCapsPatch(**campi)
                try:
                    esito = asyncio.run(AR.patch_agent_caps("avvocato", corpo, None))
                    errore = None
                except HTTPException as e:
                    esito, errore = None, e
            return esito, errore, yaml.safe_load(
                (adir / "agent.yaml").read_text()) if adir.exists() else None

    # ── il grant arriva dove si decide ───────────────────────────────────────
    def test_a_grant_is_registered_in_the_gateway_whitelist(self):
        chiamate = []
        _, err, _ = self._patch(lambda *a, **k: chiamate.append((a, k)) or {"ok": True},
                                tool_permissions=["topic.open", "topic.write_file",
                                                  "topic.write_document"])
        self.assertIsNone(err)
        self.assertEqual(len(chiamate), 1,
                         "il PATCH ha scritto la datadir senza registrare il "
                         "permesso dove viene deciso")
        self.assertEqual(chiamate[0][0][0], "avvocato")
        self.assertIn("topic.write_document", chiamate[0][0][1])

    def test_a_revocation_is_registered_too(self):
        """La direzione peggiore: un verbo tolto solo sulla carta resta attivo."""
        chiamate = []
        self._patch(lambda *a, **k: chiamate.append((a, k)) or {"ok": True},
                    tool_permissions=["topic.open"])
        self.assertEqual(len(chiamate), 1)
        self.assertNotIn("topic.write_file", chiamate[0][0][1])

    def test_only_allowed_tools_travel(self):
        """Gate e deny NON viaggiano: per il gateway l'assenza è «non mi
        pronuncio» e la lista vuota è «azzerali». Mandarli qui riscriverebbe
        controlli che questa PATCH non ha toccato — è così che sono spariti i
        gate di clodia al primo update del base-pack."""
        chiamate = []
        self._patch(lambda *a, **k: chiamate.append((a, k)) or {"ok": True},
                    tool_permissions=["topic.open"])
        self.assertEqual(chiamate[0][1], {})

    def test_a_caps_patch_without_tools_does_not_touch_the_whitelist(self):
        """Skill e rule non sono autorizzazioni del gateway: nessuna
        registrazione, nessun rischio di riscrivere `allowed_tools`."""
        chiamate = []
        with patch.object(AR, "_validate_catalog_refs", lambda items, kind: None):
            _, err, _ = self._patch(
                lambda *a, **k: chiamate.append((a, k)) or {"ok": True},
                capabilities=["dataviz"])
        self.assertIsNone(err)
        self.assertEqual(chiamate, [])

    # ── e quando il gateway non risponde ─────────────────────────────────────
    def test_a_failed_registration_is_not_an_ok(self):
        def boom(*a, **k):
            raise RuntimeError("500 Server Error for url: .../whitelist")

        esito, err, _ = self._patch(boom, tool_permissions=["topic.open", "x.y"])
        self.assertIsNone(esito)
        self.assertIsNotNone(err, "il PATCH ha risposto ok senza aver concesso")
        self.assertEqual(err.status_code, 502)
        self.assertIn("gateway", err.detail)

    def test_a_failed_registration_rolls_the_seed_back(self):
        """Dichiarazione e autorità restano d'accordo: se il gateway non ha
        registrato, il seed non deve mostrare il permesso. Un file che dichiara
        un verbo che il gateway nega è il difetto di #304 in piccolo, e nessuno
        va a cercarlo."""
        def boom(*a, **k):
            raise RuntimeError("timeout")

        _, _, su_disco = self._patch(boom, tool_permissions=["topic.open",
                                                             "topic.write_document"])
        self.assertEqual(su_disco["tool_permissions"],
                         ["topic.open", "topic.write_file"])


class TheReasonIsWrittenDownTests(unittest.TestCase):
    """Perché la ragione sta accanto al codice: senza, il prossimo che legge
    vede una POST HTTP dentro un PATCH e la classifica come best-effort — che è
    esattamente come il difetto è nato."""

    def test_the_consequence_is_next_to_the_code(self):
        import inspect
        src = inspect.getsource(AR.patch_agent_caps)
        self.assertIn("DICHIARAZIONE", src)
        self.assertIn("#304", src)

    def test_the_call_is_the_async_wrapper(self):
        """HTTP sincrono dentro un handler `async def` ferma l'event loop
        dell'intero agent-server (#106): si passa dal wrapper."""
        import inspect
        src = inspect.getsource(AR.patch_agent_caps)
        self.assertIn("register_agent_async", src)


class SetYamlListIndentTests(unittest.TestCase):
    """`_set_yaml_list` deve riconoscere una lista a indentazione ZERO come
    figlia della chiave — non solo quella indentata.

    Misurato il 13 set 2026 su `tomato.fullstack-dev` in produzione:
    `agents.grant_tool` (`github.*`) ha scritto la nuova lista indentata SOPRA
    la vecchia `- web.post` a indentazione zero, che il vecchio `\\s+` non
    riconosceva come già-presente e quindi non consumava — risultato, due
    liste per la stessa chiave, YAML non più parsabile, l'agente sparito dal
    registry (`registry.get_by_name` → `None`), 500 sul PATCH e NESSUN
    rollback per quella via (il rollback esiste solo per il fallimento della
    registrazione al gateway, non per un YAML riscritto male)."""

    def test_zero_indent_existing_list_is_replaced_not_duplicated(self):
        # Lo stile dominante in questo repo — `tool_permissions:` seguita da
        # `- x` allo STESSO livello della chiave, non indentata.
        text = "name: x\ntool_permissions:\n- web.post\nexpertise: >-\n  y\n"
        out = AR._set_yaml_list(text, "tool_permissions", ["web.post", "github.*"])
        parsed = yaml.safe_load(out)
        self.assertEqual(parsed["tool_permissions"], ["web.post", "github.*"])
        # La vecchia riga non deve sopravvivere duplicata in coda.
        self.assertEqual(out.count("web.post"), 1)

    def test_indented_existing_list_is_replaced_not_duplicated(self):
        # La forma indentata (l'unica che il vecchio regex riconosceva) deve
        # continuare a funzionare: non è un fix a senso unico.
        text = 'name: x\ntool_permissions:\n  - "web.post"\nexpertise: >-\n  y\n'
        out = AR._set_yaml_list(text, "tool_permissions", ["web.post", "github.*"])
        parsed = yaml.safe_load(out)
        self.assertEqual(parsed["tool_permissions"], ["web.post", "github.*"])
        self.assertEqual(out.count("web.post"), 1)

    def test_a_comment_immediately_above_the_key_is_preserved(self):
        # Il caso reale di `tomato.fullstack-dev`: un commento subito sopra
        # `tool_permissions:` — non deve sparire né spostarsi nella lista.
        text = ("name: x\nrules: []\n# nota sul perché\ntool_permissions:\n"
               "- web.post\nexpertise: >-\n  y\n")
        out = AR._set_yaml_list(text, "tool_permissions", ["web.post"])
        self.assertIn("# nota sul perché", out)
        parsed = yaml.safe_load(out)
        self.assertEqual(parsed["tool_permissions"], ["web.post"])

    def test_result_is_always_valid_yaml_regardless_of_source_style(self):
        # La proprietà che conta più delle altre: qualunque sia lo stile in
        # ingresso, il risultato deve sempre riparsare pulito.
        for lista in ("- a\n- b\n", "  - a\n  - b\n"):
            with self.subTest(stile=lista.splitlines()[0]):
                text = f"name: x\ncapabilities:\n{lista}rules: []\n"
                out = AR._set_yaml_list(text, "capabilities", ["a", "b", "c"])
                parsed = yaml.safe_load(out)  # solleva se non parsabile
                self.assertEqual(parsed["capabilities"], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
