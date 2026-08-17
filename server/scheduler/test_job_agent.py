"""Test del job-agent dinamico (19 giu 2026).

Copre:
  - schema job con campo `agent` (create default clodia, back-compat read, update);
  - risoluzione dinamica dei kind in sdk_runtime.session via registry seed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from . import db, scheduler
from ..agents.loader import registry
from ..agents.models import AgentSpec
from ..sdk_runtime import session as s


class JobAgentFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        db.JOBS_DIR = self._old_dir
        self._tmp.cleanup()

    def test_create_defaults_to_clodia(self) -> None:
        job = db.create_job("j1", "*/5 * * * *", "ciao")
        self.assertEqual(job["agent"], "clodia")
        self.assertEqual(db.get_job(job["id"])["agent"], "clodia")

    def test_create_with_explicit_agent(self) -> None:
        job = db.create_job("j2", "*/5 * * * *", "ciao", agent="ophelia")
        self.assertEqual(db.get_job(job["id"])["agent"], "ophelia")

    def test_legacy_job_without_agent_reads_as_looper(self) -> None:
        # Simula un job scritto prima dell'introduzione del campo `agent`.
        (db.JOBS_DIR / "7.yaml").write_text(yaml.safe_dump({
            "id": 7, "name": "vecchio", "cron_expr": "0 9 * * *",
            "prompt": "x", "enabled": True,
        }), encoding="utf-8")
        self.assertEqual(db.get_job(7)["agent"], "looper")

    def test_update_agent(self) -> None:
        job = db.create_job("j3", "*/5 * * * *", "ciao")
        db.update_job(job["id"], agent="ada")
        self.assertEqual(db.get_job(job["id"])["agent"], "ada")


class JobRunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self._tmp.name)
        self.job = db.create_job("daily", "0 8 * * *", "digest", agent="ophelia")

    async def asyncTearDown(self) -> None:
        db.JOBS_DIR = self._old_dir
        self._tmp.cleanup()

    async def test_agentic_run_transitions_from_running_to_success(self) -> None:
        run_id = db.mark_run(
            self.job["id"], status="dispatched (turno avviato in background)",
            chat_id="job:1",
        )
        self.assertIsNotNone(run_id)
        self.assertEqual(db.get_job(self.job["id"])["runs"][0]["stato"], "running")
        chat = mock.MagicMock(chat_id="job:1")
        chat.send_user_message = mock.AsyncMock(return_value="done")

        await scheduler._complete_agentic_run(self.job["id"], run_id, chat, "digest")

        stored = db.get_job(self.job["id"])
        self.assertEqual(stored["runs"][0]["stato"], "success")
        self.assertIsNotNone(stored["runs"][0]["durata"])
        self.assertEqual(stored["last_status"], "success")

    async def test_agentic_run_persists_failure(self) -> None:
        run_id = db.mark_run(
            self.job["id"], status="dispatched (turno avviato in background)",
            chat_id="job:1",
        )
        chat = mock.MagicMock(chat_id="job:1")
        chat.send_user_message = mock.AsyncMock(side_effect=RuntimeError("provider down"))

        await scheduler._complete_agentic_run(self.job["id"], run_id, chat, "digest")

        stored = db.get_job(self.job["id"])
        self.assertEqual(stored["runs"][0]["stato"], "failed")
        self.assertEqual(stored["runs"][0]["error"], "provider down")
        self.assertEqual(stored["last_status"], "failed: provider down")


class DynamicKindResolutionTests(unittest.TestCase):
    """Inietta un agent fittizio nel registry e verifica la risoluzione."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        agent_dir = Path(self._tmp.name) / "webdev"
        agent_dir.mkdir()
        self._spec = AgentSpec.model_validate({
            "name": "webdev",
            "description": "web dev",
            "model": "claude-sonnet-4-6",
            "display_name": "WebDev",
            "agent_sdk": "claude",
            "system_prompt": "system-prompt.md",
        })
        self._spec.agent_dir = str(agent_dir)
        self._codex_spec = AgentSpec.model_validate({
            "name": "saimx",
            "description": "codex dev",
            "model": "gpt-5.5",
            "display_name": "SaimX",
            "agent_sdk": "codex",
            "system_prompt": "system-prompt.md",
        })
        self._saved = dict(registry._agents)
        registry._agents["webdev"] = self._spec
        registry._agents["saimx"] = self._codex_spec

    def tearDown(self) -> None:
        registry._agents = self._saved
        self._tmp.cleanup()

    def test_known_and_available(self) -> None:
        self.assertTrue(s.known_kind("webdev"))
        self.assertTrue(s.known_kind("clodia"))     # statico
        self.assertFalse(s.known_kind("inesistente"))
        self.assertIn("webdev", s.available_kinds())
        self.assertIn("clodia", s.available_kinds())

    def test_dynamic_model_from_seed(self) -> None:
        self.assertEqual(s._resolve_model("webdev"), "claude-sonnet-4-6")
        # i kind statici restano invariati (clodia = default del CLI → None)
        self.assertIsNone(s._resolve_model("clodia"))

    def test_dynamic_cwd_from_agent_dir(self) -> None:
        self.assertEqual(str(s._resolve_cwd("webdev")), self._spec.agent_dir)

    def test_dynamic_permission_and_no_blocklist(self) -> None:
        self.assertEqual(s._resolve_permission_mode("webdev"), "bypassPermissions")
        self.assertEqual(s._resolve_disallowed_tools("webdev"), [])

    def test_runtime_from_agent_sdk(self) -> None:
        self.assertFalse(s._is_codex_kind("webdev"))   # claude
        self.assertTrue(s._is_codex_kind("saimx"))     # codex
        self.assertTrue(s._is_codex_kind("ophelia"))   # statico codex

    def test_session_construct_dynamic_kind(self) -> None:
        # La guardia non deve sollevare per un kind del registry.
        chat = s.ChatSession("c-test", kind="webdev")
        self.assertEqual(chat.kind, "webdev")
        self.assertTrue(chat.title.startswith("[WEBD]"))


class ProviderEnforcementTests(unittest.TestCase):
    """Un agent col provider scollegato non è disponibile (chat/job)."""

    def setUp(self) -> None:
        from ..agents.models import AgentSpec
        self._saved = dict(registry._agents)
        # Modello REALE e non `"m"`: `candidate_providers` filtra i provider per
        # il modello che servirebbero (`provider_supports_model`), quindi un
        # modello inventato azzera i candidati — e con zero candidati la funzione
        # ricade nel fail-open e risponde «collegato» a prescindere. Il test
        # misurava così il fallback invece della regola, ed era rosso da allora.
        registry._agents["claudette"] = AgentSpec.model_validate({
            "name": "claudette", "description": "d", "model": "claude-opus-4-8",
            "display_name": "C", "agent_sdk": "claude", "system_prompt": "s.md"})
        import server.api.providers as P
        self._P = P
        self._orig = P.connected_provider_ids

    def tearDown(self) -> None:
        registry._agents = self._saved
        self._P.connected_provider_ids = self._orig

    def test_agent_provider_resolved(self) -> None:
        # claude → [anthropic-api, claude-team, aws-region-eu, claude-pro-max];
        # nessuno collegato → il preferito, che resta il primo dell'ordine.
        self.assertEqual(s.agent_provider("claudette"), "anthropic-api")

    def test_connected_passes(self) -> None:
        self._P.connected_provider_ids = lambda: {"anthropic-api"}
        self.assertTrue(s.provider_connected_for("claudette"))
        s._ensure_provider_connected("claudette")  # non solleva

    def test_disconnected_blocks(self) -> None:
        self._P.connected_provider_ids = lambda: set()
        self.assertFalse(s.provider_connected_for("claudette"))
        with self.assertRaises(s.ProviderNotConnected):
            s._ensure_provider_connected("claudette")

    def test_unknown_provider_not_blocked(self) -> None:
        # opencode → provider non derivabile → non bloccato (fail-open).
        from ..agents.models import AgentSpec
        registry._agents["oc"] = AgentSpec.model_validate({
            "name": "oc", "description": "d", "model": "m",
            "display_name": "OC", "agent_sdk": "opencode", "system_prompt": "s.md"})
        self._P.connected_provider_ids = lambda: set()
        self.assertTrue(s.provider_connected_for("oc"))
        s._ensure_provider_connected("oc")  # non solleva


class AsyncScopeHasOneAgentTests(unittest.TestCase):
    """Uno scope asincrono ha UN agente — ed è da lì che discende R11.

    router-notebook R11: «negli scope asincroni (job) c'è un solo agent
    assegnato. I job con multipli agents non sono ancora previsti». La voce non
    descrive un comportamento: **afferma che una condizione non può presentarsi**,
    e su quell'affermazione poggia il fatto che il router non gira mai su un job.

    Era il tipo di verità che smetteva di valere in silenzio: fino al #213
    `create_job(agent=["clodia","ophelia"])` accettava la lista e la
    restituiva intatta, e il danno arrivava al fire — non come «agente non
    trovato» (che `fire_job` degrada a clodia) ma come `TypeError: unhashable
    type: 'list'` dentro `known_kind`, alle 9 del mattino, su un job accettato
    senza una parola quando è stato creato. Sul percorso `topic_trigger` era
    persino muto: la menzione finiva nel topic come `@['clodia', 'ophelia']`,
    nessun agente attivato e nessun errore.

    Ora la condizione è impedita dove si prende la decisione — in scrittura,
    con un rifiuto — e i test qui sotto sono la guardia: coprono le TRE porte
    per cui un job prende un agente (create, update, file YAML scritto a mano).
    Se un giorno il campo va allargato deliberatamente, cadono e la decisione
    torna sul tavolo invece di essere presa da un default (decision record 34).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = db.JOBS_DIR
        db.JOBS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        db.JOBS_DIR = self._old_dir
        self._tmp.cleanup()

    def test_a_job_carries_a_single_agent_name(self) -> None:
        job = db.create_job("uno", "0 9 * * *", "x", agent="clodia")
        letto = db.get_job(job["id"])["agent"]
        self.assertIsInstance(letto, str)
        self.assertEqual("clodia", letto)

    def test_creating_a_job_with_two_agents_is_refused(self) -> None:
        """R11 vive dove la decisione si prende: alla creazione, non al fire.

        Rifiuto e non normalizzazione (issue #213, opzione 2): un job lo crea
        una persona, che legge l'errore. Scegliere per lei il primo dei due
        nomi sarebbe decidere in silenzio chi risponde — che è esattamente la
        condizione che R11 dichiara impossibile.
        """
        with self.assertRaises(ValueError) as ctx:
            db.create_job("due", "0 9 * * *", "x", agent=["clodia", "ophelia"])
        self.assertIn("R11", str(ctx.exception))
        self.assertIsNone(db.get_job_by_name("due"), "il job non deve esistere")

    def test_updating_a_job_to_two_agents_is_refused(self) -> None:
        """La seconda porta di scrittura: senza questa, `update_job` rimette
        dentro la lista che `create_job` ha appena rifiutato."""
        job = db.create_job("tre", "0 9 * * *", "x", agent="clodia")
        with self.assertRaises(ValueError):
            db.update_job(job["id"], agent=["clodia", "ophelia"])
        self.assertEqual("clodia", db.get_job(job["id"])["agent"])

    def test_a_handwritten_list_is_read_as_the_first_name(self) -> None:
        """Terza porta: il file YAML si edita a mano (lo dichiara l'intestazione
        di db.py), quindi una lista può entrare senza passare da create_job.

        In lettura si COERCIZZA, non si rifiuta: far sparire da `list_jobs` un
        job programmato è un guasto peggiore di quello curato qui. L'asimmetria
        con la scrittura è voluta — in scrittura c'è qualcuno che legge
        l'errore, in lettura no: resta un warning.
        """
        (db.JOBS_DIR / "9.yaml").write_text(yaml.safe_dump({
            "id": 9, "name": "a mano", "cron_expr": "0 9 * * *",
            "prompt": "x", "enabled": True, "agent": ["clodia", "ophelia"],
        }), encoding="utf-8")
        with self.assertLogs("scheduler.db", level="WARNING") as log:
            letto = db.get_job(9)
        self.assertEqual("clodia", letto["agent"])
        self.assertIn("R11", "\n".join(log.output))

    def test_the_agentic_default_is_a_single_named_agent(self) -> None:
        """Nessun job resta senza responder: agentico senza `agent` → clodia."""
        job = db.create_job("quattro", "0 9 * * *", "x")
        self.assertEqual("clodia", db.get_job(job["id"])["agent"])


if __name__ == "__main__":
    unittest.main()
