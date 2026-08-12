"""Un seed installato ma non registrato al gateway non è «installato».

L'11 ago il gateway ha risposto 500 alla registrazione di `content-creator`
(kwarg orfano dopo un ritiro fatto a metà). Qui si scriveva un `WARNING` e si
proseguiva, la riga successiva loggava «installato **e registrato**», e l'import
rispondeva 200. Nel pannello l'agente compariva completo di verbi — che sono
quelli DICHIARATI nel seed, non quelli che il gateway gli concede.

Il seguito è costato due ore: l'agente entrava nei canali, parlava, e diceva di
non avere nessun tool `topic.*`. Un altro agente ha controllato la scheda, l'ha
trovata corretta, ha riavviato la sessione e ha concluso che fosse il runtime.
Nessuno guardava dove la catena si fosse spezzata, perché la catena aveva
dichiarato di essere intera.

Non serve un log più educato: serve che l'ESITO porti la differenza.
"""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import pack_import as PI


SEED = {
    "name": "content-creator",
    "description": "seed di prova",
    "type": "normal",
    "clearance": "SEAL-1",
    "agent_sdk": "claude",
    "model": "claude-sonnet-5",
    "tool_permissions": ["topic.open", "topic.files"],
}


class _Registry:
    """Registry finto: accetta il seed copiato e ne restituisce lo spec."""

    def __init__(self, base: Path):
        self.base_dir = base

    def load(self):
        return None

    def get_by_name(self, name):
        from types import SimpleNamespace
        return SimpleNamespace(tool_permissions=SEED["tool_permissions"],
                               gated_tools=None, gated_in_channel=None,
                               profile_tools=None, carries=None,
                               denied_tools=["topic.read_file"])

    def errors(self):
        return {}


class InstallSeedTests(unittest.TestCase):
    """Esercita `_install_seed` per davvero: copia un seed su disco e sostituisce
    solo i due lati esterni (PKI e gateway)."""

    def _install(self, register):
        from . import gateway_admin
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src" / "content-creator"
            src.mkdir(parents=True)
            (src / "agent.yaml").write_text(yaml.safe_dump(SEED), encoding="utf-8")
            base = Path(tmp) / "agents"
            base.mkdir()
            with patch.object(PI, "registry", _Registry(base)), \
                    patch.object(gateway_admin, "register_agent", register), \
                    patch("server.colony.pki.issue_agent_identity", lambda n: None):
                return PI._install_seed(src)

    def test_a_failed_registration_is_reported_in_the_result(self):
        def boom(*a, **k):
            raise RuntimeError("500 Server Error for url: .../whitelist")

        out = self._install(boom)
        self.assertEqual(out["status"], "installed")
        self.assertIn("warning", out)
        self.assertIn("gateway", out["warning"])
        self.assertIn("nessun verbo", out["warning"])

    def test_a_good_registration_carries_no_warning(self):
        out = self._install(lambda *a, **k: {"ok": True})
        self.assertEqual(out["status"], "installed")
        self.assertNotIn("warning", out)

    def test_registration_transports_denied_tools(self):
        calls = []
        self._install(lambda *a, **k: calls.append((a, k)) or {"ok": True})
        self.assertEqual(calls[0][1]["denied_tools"], ["topic.read_file"])

    def test_the_seed_lands_on_disk_either_way(self):
        """L'avviso non annulla l'installazione: il seed c'è, gli manca la
        registrazione — ed è precisamente ciò che il chiamante deve sapere per
        rimediare senza reinstallare."""
        def boom(*a, **k):
            raise RuntimeError("500")

        out = self._install(boom)
        self.assertEqual(out["name"], "content-creator")


class TheWarningReachesTheTopTests(unittest.TestCase):
    def test_the_import_aggregates_seed_warnings(self):
        src = inspect.getsource(PI.install_pack_from_root)
        self.assertIn('a.get("warning")', src)
        self.assertIn('out["warnings"]', src)


class TheReasonIsWrittenDownTests(unittest.TestCase):
    """Perché l'avviso conta: la config mancante non degrada, spegne. Se questa
    ragione sparisce dal codice, il prossimo che legge riclassifica la
    registrazione come best-effort e il difetto torna."""

    def test_the_consequence_is_next_to_the_code(self):
        src = inspect.getsource(PI._install_seed)
        self.assertIn("VUOTA", src)


if __name__ == "__main__":
    unittest.main()
