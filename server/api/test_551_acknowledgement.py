"""clodia-platform#551: a bare acknowledgement starts no agent turn.

SEAL-1/software-house: fullstack-dev answered every clodia message with
«@clodia Ok / Ricevuto / resto disponibile», clodia replied «Nessuna azione
richiesta», and the chain burned its 15 delegation hops on nothing. The same
rule holds for people: «grazie» does not wake the coordinator to say «prego».
The message is still stored and shown; only the turn is skipped.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import acknowledgement, channels, router_config
from .test_channels import _a
from .test_r3_one_mention import _Base


ACKS = [
    "ok", "OK", "Ok.", "okay!", "okk", "ricevuto", "Ricevuto, grazie!",
    "grazie", "Grazie mille 🙏", "perfetto", "va bene", "d’accordo", "capito",
    "👍", "👍🏻", "👌", "✅✅", "🙏 grazie", "thanks", "Thank you!", "got it",
    "noted", "sounds good", "@clodia ok", "@clodia Ok / Ricevuto / resto disponibile",
    "Nessuna azione richiesta.", "@worker 👍",
]

NEAR_MISSES = [
    "ok, procedi con il preventivo",
    "ricevuto?",
    "ok?",
    "ok fai X",
    "ok fai la bozza",
    "grazie, ora manda la mail a Mario",
    "sì",               # a go-ahead after «Procedo?», not an acknowledgement
    "yes",
    "procedi",
    "fatto",
    "@clodia",          # a summons with no text is not a thank-you
    "@clodia ok, ma controlla le date",
    "ok 👍 e aggiorna il summary",
    "",
    "ok " + "grazie " * 20,   # too long to be a bare acknowledgement
    "👍 ?",
]


class ClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = acknowledgement.AckConfig()

    def test_acknowledgements_are_recognised(self) -> None:
        for text in ACKS:
            with self.subTest(text=text):
                self.assertTrue(acknowledgement.is_bare_acknowledgement(
                    text, config=self.cfg))

    def test_near_misses_are_not(self) -> None:
        for text in NEAR_MISSES:
            with self.subTest(text=text):
                self.assertFalse(acknowledgement.is_bare_acknowledgement(
                    text, config=self.cfg))

    def test_an_attachment_is_content(self) -> None:
        self.assertFalse(acknowledgement.is_bare_acknowledgement(
            "ok", ["fattura.pdf"], config=self.cfg))
        self.assertFalse(acknowledgement.is_bare_acknowledgement(
            "", ["fattura.pdf"], config=self.cfg))

    def test_disabled_routes_everything(self) -> None:
        off = acknowledgement._validated({"enabled": False})
        self.assertFalse(acknowledgement.is_bare_acknowledgement("ok", config=off))


class ConfigTests(unittest.TestCase):
    """The lists live in router.yaml and an instance file overrides them live."""

    def _load_with(self, repo: str | None, live: str | None):
        with tempfile.TemporaryDirectory() as d:
            rp, lp = Path(d, "repo.yaml"), Path(d, "live.yaml")
            if repo is not None:
                rp.write_text(repo, encoding="utf-8")
            if live is not None:
                lp.write_text(live, encoding="utf-8")
            with patch.object(router_config, "_REPO_PATH", rp), \
                    patch.object(router_config, "_LIVE_PATH", lp), \
                    patch.object(acknowledgement, "_CACHE_KEY", None):
                return acknowledgement.load()

    def test_the_versioned_catalog_parses_and_keeps_defaults(self) -> None:
        cfg = self._load_with(router_config._REPO_PATH.read_text(encoding="utf-8"), None)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.max_chars, 60)
        self.assertIn("ricevuto", cfg.phrases)
        self.assertIn("👍", cfg.emoji)

    def test_an_instance_list_replaces_the_default(self) -> None:
        cfg = self._load_with(
            "acknowledgements:\n  enabled: true\n",
            "acknowledgements:\n  phrases: [\"vabbè\", \"Ci sta\"]\n")
        self.assertTrue(acknowledgement.is_bare_acknowledgement("ci sta", config=cfg))
        self.assertTrue(acknowledgement.is_bare_acknowledgement("vabbè!", config=cfg))
        self.assertFalse(acknowledgement.is_bare_acknowledgement("ricevuto", config=cfg))
        # patterns and emoji keep their defaults
        self.assertTrue(acknowledgement.is_bare_acknowledgement("okk", config=cfg))
        self.assertTrue(acknowledgement.is_bare_acknowledgement("👍", config=cfg))

    def test_the_instance_can_turn_it_off(self) -> None:
        cfg = self._load_with(None, "acknowledgements:\n  enabled: false\n")
        self.assertFalse(acknowledgement.is_bare_acknowledgement("ok", config=cfg))

    def test_an_invalid_section_falls_back_to_defaults(self) -> None:
        cfg = self._load_with(None, "acknowledgements:\n  patterns: [\"(\"]\n")
        self.assertEqual([p.pattern for p in cfg.patterns],
                         list(acknowledgement.DEFAULT_PATTERNS))
        self.assertTrue(acknowledgement.is_bare_acknowledgement("ok", config=cfg))


class AHumanAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    """The human path of `post_channel_message`."""

    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self.posts: list[tuple] = []
        os.environ.pop("CHANNEL_MULTI_RESPONDER", None)

    @contextlib.contextmanager
    def _channel(self, start):
        def post(_tier, _name, author, text, kind="human", **kw):
            self.posts.append((author, text, kind, kw.get("attachments")))
            return {"id": str(len(self.posts))}

        with contextlib.ExitStack() as stack:
            for cm in (
                patch.object(channels.registry, "get_by_name",
                             side_effect=lambda n: self.agents.get(n)),
                patch.object(channels, "_track_routing_decision", lambda _p: None),
                patch.object(channels, "_provider_seal_ok", return_value=True),
                patch.object(channels.topics_client, "open_topic", return_value={
                    "meta": {"tier": "P0",
                             "participants": ["owner", "clodia", "worker"]}}),
                patch.object(channels.topics_client, "post_message", side_effect=post),
                patch.object(channels.topics_client, "list_messages", return_value=[]),
                patch.object(channels.access_log, "touch", lambda *a, **k: None),
                patch.object(channels.activity_log, "append", lambda *a, **k: None),
                patch.object(channels, "_channel_message", AsyncMock()),
                patch.object(channels, "_start_turn", start),
                patch.object(channels, "_routing_plan",
                             return_value=[(self.agents["clodia"], "x")]),
            ):
                stack.enter_context(cm)
            yield

    async def _post(self, text, attachments=None):
        start = AsyncMock(return_value=True)
        with self._channel(start):
            result = await channels.post_channel_message(
                "P0", "ops", text, "owner", attachments=attachments)
        return result, start

    async def test_acknowledgements_do_not_route_but_are_stored(self) -> None:
        for text in ("ok", "ricevuto", "grazie", "👍", "@worker ok", "@clodia ricevuto, grazie"):
            with self.subTest(text=text):
                self.posts.clear()
                result, start = await self._post(text)
                start.assert_not_awaited()
                self.assertTrue(result["posted"])
                self.assertTrue(result.get("acknowledgement"))
                self.assertEqual([(a, t) for a, t, _k, _x in self.posts],
                                 [("owner", text)], "the message is still stored")

    async def test_near_misses_route(self) -> None:
        for text in ("ok, procedi con il preventivo", "ricevuto?", "ok fai X"):
            with self.subTest(text=text):
                result, start = await self._post(text)
                self.assertEqual(1, start.await_count)
                self.assertNotIn("acknowledgement", result)

    async def test_a_mention_with_real_content_still_starts_the_turn(self) -> None:
        result, start = await self._post("@worker ok, prepara la bozza")
        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)

    async def test_an_attachment_routes_and_is_stored_with_the_message(self) -> None:
        result, start = await self._post("ok", attachments=["fattura.pdf"])
        self.assertEqual(1, start.await_count)
        self.assertEqual(self.posts[-1][3], ["fattura.pdf"])

    async def test_trusted_internal_triggers_are_not_filtered(self) -> None:
        start = AsyncMock(return_value=True)
        with self._channel(start):
            await channels.post_channel_message(
                "P0", "ops", "ok", "scheduler", kind="system", trusted_internal=True)
        self.assertEqual(1, start.await_count)


class AnAgentAcknowledgementTests(_Base):
    """The loop of the issue: an agent's «@clodia Ok / Ricevuto» is no delegation."""

    async def _delegate(self, from_agent, text):
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(["owner", "clodia", "worker"])
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate("P0", "ops", from_agent, text, "owner", 0)
        return posts, start

    async def test_an_acknowledging_mention_starts_nobody(self) -> None:
        posts, start = await self._delegate(
            "worker", "@clodia Ok / Ricevuto / resto disponibile")
        start.assert_not_awaited()
        self.assertEqual([], posts, "no router notice either")

    async def test_the_limit_is_not_reached_by_acknowledgements(self) -> None:
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(["owner", "clodia", "worker"])
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate(
                "P0", "ops", "worker", "@clodia ricevuto 👍", "owner",
                channels._max_delegation_hops())
        start.assert_not_awaited()
        self.assertEqual([], posts, "no delegation_limit notice for a thank-you")

    async def test_a_real_request_still_delegates(self) -> None:
        _posts, start = await self._delegate("worker", "@clodia ok, serve il tuo via libera?")
        self.assertEqual(1, start.await_count)
        self.assertEqual("clodia", start.await_args.args[3].name)


class ReportBackTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, esito):
        turns: list = []

        async def start_turn(*a, **kw):
            turns.append(a[3].name)
            return True

        class _Spec:
            name, type = "clodia", "bot"

        class _Chat:
            origin = ["human:davide", "agent:clodia", "agent:worker"]

        with patch.object(channels, "_start_turn", start_turn), \
                patch.object(channels.registry, "get_by_name",
                             lambda n: _Spec() if n == "clodia" else None), \
                patch.object(channels.topics_client, "open_topic",
                             lambda *_a, **_k: {"meta": {
                                 "tier": "P0", "participants": ["clodia", "worker"]}}), \
                patch.object(channels, "_provider_seal_ok", lambda *_a: True):
            await channels._report_back("P0", "ops", "worker", _Chat(), esito, 0)
        return turns

    async def test_an_acknowledgement_is_not_reported_back(self) -> None:
        self.assertEqual([], await self._run("Ok, ricevuto. Resto in attesa."))

    async def test_a_real_outcome_is(self) -> None:
        self.assertEqual(["clodia"], await self._run("Bozza pronta in files/bozza.md"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
