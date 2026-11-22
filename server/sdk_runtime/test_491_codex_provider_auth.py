"""clodia-platform#491 — the codex runtime uses the credential of its provider.

`CodexChatSession` ran every turn on the ChatGPT login, whatever provider
resolution had picked: an API-key provider was only logged, and a model the
subscription rejects came back as an upstream 400. Owner's policy (2 Oct
2026): codex agents stay on the subscription, no pay-per-use by default. So:

- `codex` (or nothing) resolved → the subscription, exactly as before;
- an API-key provider the SEED declares → that provider's key in a dedicated
  CODEX_HOME (no ChatGPT auth.json, `forced_login_method = "api"`), file 0600
  owned by the spawn uid (as #706), the key never in argv, env or logs;
- an API-key provider the seed does NOT declare → the subscription when it can
  serve the turn, otherwise a refusal in the room naming model, provider and
  the cure — never an upstream 400.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from . import session as S
from ..api import providers as P

SECRET = "sk-test-491-DO-NOT-LEAK"


class _Done(Exception):
    pass


def _spec(providers=None, provider=None):
    return SimpleNamespace(providers=providers, provider=provider, agent_sdk="codex",
                           model="gpt-6-astra", provider_models=None)


class _PlanPatches:
    """Patches for `_codex_auth_plan`: resolved provider, seed, connected set."""

    def __init__(self, resolved, spec, connected=("codex", "openai-api"),
                 paused=(), model="gpt-6-astra"):
        self.patches = [
            mock.patch.object(S, "_runtime_provider", lambda k, o: (o or {}).get("provider") or resolved),
            mock.patch.object(S, "_kind_spec", lambda k: spec),
            mock.patch.object(S, "_runtime_model", lambda k, o: model),
            mock.patch.object(P, "connected_provider_ids", lambda: set(connected)),
            mock.patch.object(P, "provider_paused", lambda pid: pid in paused),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


class AuthPlanTests(unittest.TestCase):
    def test_codex_resolved_is_the_subscription(self) -> None:
        with _PlanPatches("codex", _spec(["codex"])):
            plan = S._codex_auth_plan("ophelia", {})
        self.assertEqual((plan.provider, plan.auth), ("codex", S.CODEX_AUTH_SUBSCRIPTION))

    def test_unresolved_is_the_subscription(self) -> None:
        with _PlanPatches(None, None):
            plan = S._codex_auth_plan("ophelia", {})
        self.assertEqual(plan.auth, S.CODEX_AUTH_SUBSCRIPTION)

    def test_a_non_codex_provider_answer_is_the_path_of_before(self) -> None:
        # e.g. the fail-open answer of a resolution that could not read the vault
        with _PlanPatches("anthropic-api", None):
            plan = S._codex_auth_plan("ophelia", {})
        self.assertEqual((plan.provider, plan.auth), ("codex", S.CODEX_AUTH_SUBSCRIPTION))

    def test_declared_api_provider_runs_with_its_key(self) -> None:
        with _PlanPatches("openai-api", _spec(["openai-api", "codex"])):
            plan = S._codex_auth_plan("fullstack-dev", {})
        self.assertEqual((plan.provider, plan.auth), ("openai-api", S.CODEX_AUTH_API_KEY))

    def test_legacy_single_provider_alias_is_a_declaration(self) -> None:
        with _PlanPatches("openai-api", _spec(provider="openai")):
            plan = S._codex_auth_plan("fullstack-dev", {})
        self.assertEqual(plan.auth, S.CODEX_AUTH_API_KEY)

    def test_topic_override_on_a_declared_provider_runs_with_its_key(self) -> None:
        with _PlanPatches("codex", _spec(["codex", "openai-api"])):
            plan = S._codex_auth_plan("fullstack-dev",
                                      {"provider": "openai-api", "topic_tier": "SEAL-1"})
        self.assertEqual((plan.provider, plan.auth), ("openai-api", S.CODEX_AUTH_API_KEY))

    def test_implicit_api_provider_stays_on_a_usable_subscription(self) -> None:
        # SDK defaults put openai-api first (and the topic picks it as min-cost):
        # not a declaration, so the turn stays on the subscription.
        with _PlanPatches("openai-api", _spec()):
            plan = S._codex_auth_plan("ophelia", {"provider": "openai-api",
                                                  "topic_tier": "SEAL-1"})
        self.assertEqual((plan.provider, plan.auth), ("codex", S.CODEX_AUTH_SUBSCRIPTION))
        self.assertEqual(plan.resolved, "openai-api")

    def test_implicit_mismatch_without_subscription_is_refused(self) -> None:
        with _PlanPatches("openai-api", _spec(), connected=("openai-api",)):
            plan = S._codex_auth_plan("ophelia", {})
        self.assertEqual(plan.auth, S.CODEX_AUTH_REFUSED)
        self.assertEqual(plan.provider, "openai-api")
        for needle in ("ophelia", "gpt-6-astra", "`openai-api`", "non è collegato",
                       "collegare l'abbonamento Codex/ChatGPT",
                       "providers: [openai-api]"):
            self.assertIn(needle, plan.refusal)

    def test_implicit_mismatch_on_a_model_the_subscription_rejects_is_refused(self) -> None:
        with _PlanPatches("openai-api", _spec(), model="gpt-5-codex"):
            plan = S._codex_auth_plan("ophelia", {})
        self.assertEqual(plan.auth, S.CODEX_AUTH_REFUSED)
        self.assertIn("non accetta il modello `gpt-5-codex`", plan.refusal)
        self.assertIn("un modello che l'abbonamento ChatGPT accetta", plan.refusal)

    def test_implicit_mismatch_with_the_subscription_paused_is_refused(self) -> None:
        with _PlanPatches("openai-api", _spec(), paused=("codex",)):
            plan = S._codex_auth_plan("ophelia", {})
        self.assertEqual(plan.auth, S.CODEX_AUTH_REFUSED)
        self.assertIn("è in pausa", plan.refusal)


class CodexApiKeyTests(unittest.TestCase):
    def test_key_comes_from_the_provider_env(self) -> None:
        with mock.patch.object(P, "provider_env",
                               lambda pid=None: {"OPENAI_API_KEY": SECRET}) as _:
            self.assertEqual(P.codex_api_key("openai-api"), SECRET)
            self.assertEqual(P.codex_api_key("openai"), SECRET)      # legacy alias
            self.assertIsNone(P.codex_api_key("codex"))              # subscription
            self.assertIsNone(P.codex_api_key("anthropic-api"))      # not codex
            self.assertIsNone(P.codex_api_key("generic-openai"))     # opencode

    def test_no_key_is_none(self) -> None:
        with mock.patch.object(P, "provider_env", lambda pid=None: {}):
            self.assertIsNone(P.codex_api_key("openai-api"))


def _session(tmp: Path, *, spawn: bool = True) -> S.CodexChatSession:
    sess = object.__new__(S.CodexChatSession)
    sess.kind = "fullstack-dev"
    sess.chat_id = "chan:SEAL-1:t:fullstack-dev"
    sess.principal = "davide"
    sess._spawn = None
    sess._spawn_dir = (tmp / "spawn") if spawn else None
    if spawn:
        sess._spawn_dir.mkdir()
    sess._codex_home = None
    sess._runtime_override = {}
    sess._sandbox_uid = None
    sess._sandbox_argv = []
    sess._sandbox_env = {}
    sess._sandbox_groups = ()
    sess._api_home_tmpdir = None
    sess._thread_id = None
    sess._thread_home = None
    sess._last_usage = {}
    sess._context_tokens = 0
    sess._codex_cmd = lambda model: ["codex", "exec", "--json", "-"]
    return sess


class _TurnHarness:
    """Runs `_run_turn` up to the subprocess, capturing argv/env and logs."""

    def __init__(self, test: unittest.TestCase, sess, plan, *, sandbox=True,
                 subscription_home: Path | None = None):
        self.test, self.sess, self.plan = test, sess, plan
        self.sandbox, self.subscription_home = sandbox, subscription_home
        self.seen: dict = {}
        self.prepare_calls: list = []
        self.shared: list = []
        self.chowns: list = []
        self.activity: list = []

    def run(self):
        seen = self.seen

        async def capture(*argv, **kw):
            seen["argv"], seen["env"] = list(argv), kw["env"]
            raise _Done

        def prepare(kind, spawn_dir, real, *, groups=(), umask=""):
            self.prepare_calls.append(groups)
            return (20001, ["/clodia/docker/agent-sandbox-exec.sh"],
                    {"CLODIA_AGENT_UID": "20001", "CLODIA_AGENT_GID": "22001",
                     "CLODIA_REAL_CLI": "/usr/bin/codex", "HOME": str(spawn_dir)})

        def fchown(fd, uid, gid):
            self.chowns.append(("fchown", uid, gid))

        def chown(path, uid, gid, **kw):
            self.chowns.append(("chown", uid, gid))

        def append(agent, typ, payload, *a, **k):
            self.activity.append((typ, payload))

        patches = [
            mock.patch.object(S, "_codex_auth_plan", lambda k, o: self.plan),
            mock.patch.object(S, "_runtime_model", lambda k, o: "gpt-6-astra"),
            mock.patch.object(S, "_sandbox_enabled", lambda k: self.sandbox),
            mock.patch.object(S, "_sandbox_prepare", prepare),
            mock.patch.object(S, "_seed_gid", lambda k: 22001),
            mock.patch.object(S, "_share_codex_home", lambda h: self.shared.append(h)),
            mock.patch.object(S.pki, "mint_session_token", lambda *a, **k: "ckt1-tok"),
            mock.patch.object(S.activity_log, "append", append),
            mock.patch.object(P, "provider_env",
                              lambda pid=None: {"OPENAI_API_KEY": SECRET}),
            mock.patch.object(P, "codex_home", lambda: self.subscription_home),
            mock.patch("os.fchown", fchown),
            mock.patch("os.chown", chown),
            mock.patch("asyncio.create_subprocess_exec", capture),
        ]
        for p in patches:
            p.start()
        try:
            with self.test.assertLogs(level="DEBUG") as logs:
                logging.getLogger(S.__name__).info("harness start")
                try:
                    asyncio.run(self.sess._run_turn("ciao"))
                except _Done:
                    pass
        finally:
            for p in reversed(patches):
                p.stop()
        self.logs = "\n".join(logs.output)
        return self


class SubscriptionPathUnchangedTests(unittest.TestCase):
    def test_subscription_turn_uses_the_shared_home_and_its_group(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "codex-home"
        home.mkdir()
        sess = _session(Path(tmp.name))
        plan = S.CodexAuthPlan("codex", S.CODEX_AUTH_SUBSCRIPTION, "codex")
        h = _TurnHarness(self, sess, plan, subscription_home=home).run()
        self.assertEqual(h.seen["env"]["CODEX_HOME"], str(home))
        self.assertEqual(h.prepare_calls, [(S._CODEX_HOME_GID,)])
        self.assertIn(home, h.shared)                       # re-shared before the turn
        self.assertFalse((sess._spawn_dir / S.CODEX_API_HOME_NAME).exists())
        self.assertNotIn("forced_login_method", (home / "config.toml").read_text())
        self.assertIn("provider=codex auth=chatgpt-subscription", h.logs)
        started = [p for t, p in h.activity if t == "run_started"][0]
        self.assertEqual((started["provider"], started["auth"]),
                         ("codex", "chatgpt-subscription"))


class ApiKeyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.sess = _session(self.tmp)
        self.plan = S.CodexAuthPlan("openai-api", S.CODEX_AUTH_API_KEY, "openai-api")

    def test_dedicated_home_with_api_login_and_the_provider_key(self) -> None:
        h = _TurnHarness(self, self.sess, self.plan).run()
        home = self.sess._spawn_dir / S.CODEX_API_HOME_NAME
        self.assertEqual(h.seen["env"]["CODEX_HOME"], str(home))
        auth = json.loads((home / "auth.json").read_text())
        self.assertEqual(auth, {"auth_mode": "apikey", "OPENAI_API_KEY": SECRET})
        self.assertNotIn("tokens", auth)                    # no ChatGPT login in it
        config = (home / "config.toml").read_text()
        self.assertTrue(config.startswith('forced_login_method = "api"\n'))
        self.assertIn("[mcp_servers.clodia-tools]", config)
        for name in ("auth.json", "config.toml"):
            self.assertEqual(stat.S_IMODE((home / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        # files and dir handed to the spawn uid (seed gid), as for #706
        self.assertEqual(h.chowns.count(("fchown", 20001, 22001)), 2)
        self.assertIn(("chown", 20001, 22001), h.chowns)
        # no shared subscription home, no CODEX_HOME group for this uid
        self.assertEqual(h.shared, [])
        self.assertEqual(h.prepare_calls, [()])

    def test_the_key_is_never_in_argv_env_or_logs(self) -> None:
        h = _TurnHarness(self, self.sess, self.plan).run()
        self.assertNotIn(SECRET, " ".join(h.seen["argv"]))
        self.assertNotIn(SECRET, json.dumps(h.seen["env"]))
        self.assertNotIn(SECRET, h.logs)
        self.assertNotIn(SECRET, json.dumps(h.activity))
        self.assertIn("provider=openai-api auth=api-key", h.logs)

    def test_missing_key_is_a_refusal_not_a_subscription_turn(self) -> None:
        with mock.patch.object(P, "codex_api_key", lambda pid: None):
            with self.assertRaises(S.CodexProviderRefused) as cm:
                self.sess._prepare_api_home("openai-api")
        self.assertIn("chiave API", str(cm.exception))

    def test_a_thread_of_another_home_is_not_resumed(self) -> None:
        self.sess._thread_id = "thread-from-subscription"
        self.sess._thread_home = self.tmp / "codex-home"
        with mock.patch.object(S.CodexChatSession, "_restore_codex_thread",
                               lambda s, t: setattr(s, "_thread_id", t)):
            _TurnHarness(self, self.sess, self.plan).run()
        self.assertIsNone(self.sess._thread_id)
        self.assertEqual(self.sess._thread_home,
                         self.sess._spawn_dir / S.CODEX_API_HOME_NAME)

    def test_without_a_spawn_dir_the_home_is_private_and_dies_with_stop(self) -> None:
        sess = _session(self.tmp, spawn=False)
        _TurnHarness(self, sess, self.plan, sandbox=False).run()
        home = Path(sess._codex_home)
        self.assertTrue((home / "auth.json").is_file())
        self.assertEqual(stat.S_IMODE(home.parent.stat().st_mode), 0o700)
        tmpdir = sess._api_home_tmpdir
        sess._proc = None
        sess._client = object()
        with mock.patch.object(S.CodexChatSession, "_set_status",
                               mock.AsyncMock()):
            asyncio.run(sess.stop())
        self.assertFalse(Path(tmpdir).exists())


class RefusalTests(unittest.TestCase):
    def test_implicit_mismatch_refuses_the_turn_with_a_room_note(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sess = _session(Path(tmp.name))
        with _PlanPatches("openai-api", _spec(), connected=("openai-api",)):
            plan = S._codex_auth_plan("fullstack-dev", {})
        sess._run_codex_once = mock.AsyncMock()
        activity: list = []
        with mock.patch.object(S, "_codex_auth_plan", lambda k, o: plan), \
                mock.patch.object(S.activity_log, "append",
                                  lambda a, t, p, *x, **k: activity.append((t, p))):
            with self.assertRaises(S.CodexProviderRefused) as cm:
                asyncio.run(sess._run_turn("ciao"))
        sess._run_codex_once.assert_not_awaited()          # never reaches upstream
        err = cm.exception
        self.assertEqual(err.audit_cause, "provider_refused")
        self.assertTrue(err.nota_utente.startswith("⛔ turno di **fullstack-dev**"))
        self.assertIn("providers: [openai-api]", err.nota_utente)
        # the room shows the note, not a repr
        from ..api import channels
        self.assertEqual(channels._diagnosi(err), err.nota_utente)
        from .. import audit_events
        self.assertEqual(audit_events.failure_class(err), "provider_refused")
        types = [t for t, _ in activity]
        self.assertEqual(types, ["run_started", "error"])
        self.assertEqual(activity[0][1]["auth"], "refused")


class StartTests(unittest.TestCase):
    def _start(self, plan, subscription_home):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sess = S.CodexChatSession.__new__(S.CodexChatSession)
        sess.chat_id = "c491"
        sess.kind = "fullstack-dev"
        sess._runtime_override = {}
        sess._codex_home = None
        sess._thread_id = None
        sess._thread_home = None
        spawn = Path(tmp.name) / "spawn"
        spawn.mkdir()
        with mock.patch.object(S, "_codex_auth_plan", lambda k, o: plan), \
                mock.patch.object(P, "codex_home", lambda: subscription_home), \
                mock.patch.object(S, "_sandbox_enabled", lambda k: False), \
                mock.patch.object(S, "_materialize_spawn", lambda k, o: (None, spawn)), \
                mock.patch.object(S, "_resolve_sessions_dir", lambda k: Path(tmp.name) / "s"), \
                mock.patch.object(S, "_resolve_native_denied", lambda k: []), \
                mock.patch.object(S, "_resolve_native_allowed", lambda k: None), \
                mock.patch.object(S.CodexChatSession, "_set_status", mock.AsyncMock()), \
                mock.patch.dict(S.KIND_AUTO_INTRO, {}, clear=True):
            asyncio.run(sess.start())
        return sess

    def test_an_api_key_session_starts_without_the_subscription(self) -> None:
        sess = self._start(S.CodexAuthPlan("openai-api", S.CODEX_AUTH_API_KEY), None)
        self.assertIsNotNone(sess._client)
        self.assertIsNone(sess._codex_home)

    def test_a_subscription_session_still_needs_the_subscription(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non connesso"):
            self._start(S.CodexAuthPlan("codex", S.CODEX_AUTH_SUBSCRIPTION), None)


if __name__ == "__main__":
    unittest.main()
