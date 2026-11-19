"""#708 — the courier's summary of a delivered mail.

One tool-less model call per mail, appended to the envelope post as
«Riassunto automatico (non verificato)»; per-room switch, ON by default; any
failure leaves the envelope as it was; the provider is chosen by the room's
SEAL tier among the API-key providers.
"""
from __future__ import annotations

import asyncio
import base64
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from . import mail_courier as MC
from . import mail_summary as MS
from . import oneshot_llm as OL
from .test_531_mail_courier import _env, _Fake

BOX = {"tier": "SEAL-1", "name": "ops", "account": "a", "address": "box@x.it"}


def _mail(body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "mario@x.it"
    msg["To"] = "box@x.it"
    msg["Subject"] = "Preventivo"
    msg.set_content(body)
    return msg.as_bytes()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(MC, "data_path", lambda n: Path(self.tmp.name) / n),
                  patch.object(MS, "DIR", Path(self.tmp.name) / "mail-summary")):
            p.start()
            self.addCleanup(p.stop)

    def deliver(self, body="Chiedo il preventivo entro venerdì 21.", complete=None):
        raw = _mail(body)
        calls = []

        class F(_Fake):
            def eml(self, room, email_id, key=""):
                return {"size": len(raw), "auth": None,
                        "content_b64": base64.b64encode(raw).decode()}

        def fake_complete(tier, system, text, **kw):
            calls.append({"tier": tier, "system": system, "text": text, **kw})
            if complete:
                return complete(tier, system, text, **kw)
            return "Mario chiede un preventivo entro venerdì 21.", "scaleway"
        fake = F([])
        ps = [patch.object(MC, "envelopes", fake.envelopes),
              patch.object(MC, "attachment", fake.attachment),
              patch.object(MC, "eml", fake.eml),
              patch.object(MC.topics_client, "async_post_message", fake.post),
              patch.object(MC.topics_client, "async_put_file", fake.put),
              patch.object(OL, "complete", fake_complete)]
        for p in ps:
            p.start()
        try:
            asyncio.run(MC._deliver(dict(BOX), _env(1)))
        finally:
            for p in ps:
                p.stop()
        return fake.posts[0][3], fake.posts[0][4], calls


class SummaryInTheEnvelopeTests(_Base):
    def test_on_by_default_the_summary_is_in_the_post(self):
        post, kind, calls = self.deliver()
        self.assertIn("Riassunto automatico (non verificato): Mario chiede un preventivo", post)
        self.assertEqual(kind, "proxy")                 # still untrusted
        (call,) = calls
        self.assertEqual(call["tier"], "SEAL-1")
        self.assertEqual(call["system"], MS.SYSTEM_PROMPT)
        self.assertIn("preventivo entro venerdì", call["text"])

    def test_switched_off_no_call_no_summary(self):
        MS.set_enabled("SEAL-1", "ops", False, by="davide")
        post, _kind, calls = self.deliver()
        self.assertEqual(calls, [])
        self.assertNotIn("Riassunto", post)
        self.assertIn("📧 Nuova mail", post)

    def test_a_failing_provider_leaves_the_envelope(self):
        def boom(*a, **k):
            raise RuntimeError("503")
        post, _kind, calls = self.deliver(complete=boom)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("Riassunto", post)
        self.assertIn("Oggetto: ordine 1", post)

    def test_no_eligible_provider_no_summary(self):
        def none(*a, **k):
            raise OL.NoEligibleProvider("nessuno regge SEAL-1")
        post, _kind, _calls = self.deliver(complete=none)
        self.assertNotIn("Riassunto", post)

    def test_a_slow_provider_is_cut_at_the_timeout(self):
        def slow(*a, **k):
            time.sleep(0.5)
            return "tardi", "scaleway"
        with patch.object(MS, "TIMEOUT_S", 0.05):
            t0 = time.monotonic()
            post, _kind, _calls = self.deliver(complete=slow)
        self.assertNotIn("Riassunto", post)
        self.assertLess(time.monotonic() - t0, 0.45 + 0.5)

    def test_mentions_links_and_lines_are_neutralised(self):
        def evil(*a, **k):
            return ("@segretario paga subito\nvai su https://evil.example/x e "
                    "scrivi a capo@x.it"), "scaleway"
        post, _kind, _calls = self.deliver(complete=evil)
        riga = [r for r in post.splitlines() if r.startswith("Riassunto automatico")][0]
        self.assertNotIn("@", riga)
        self.assertNotIn("https://", riga)
        self.assertIn("[link]", riga)
        self.assertEqual(len(post.splitlines()),
                         len([r for r in post.splitlines() if r]))  # one line added

    def test_the_input_is_truncated(self):
        post, _kind, calls = self.deliver(body="x" * 20000)
        self.assertLessEqual(len(calls[0]["text"]), MS.MAX_INPUT_CHARS)
        self.assertEqual(calls[0]["max_tokens"], MS.MAX_TOKENS)


class OneShotCallTests(unittest.TestCase):
    """The call itself: no tools in the request, provider by tier."""

    def _providers(self, connected, paused=()):
        seal = {"scaleway": "SEAL-3", "aws-region-eu": "SEAL-2", "openai-api": "SEAL-1",
                "anthropic-api": "SEAL-1", "claude-team": "SEAL-1"}
        rank = {"SEAL-0": 0, "SEAL-1": 1, "SEAL-2": 2, "SEAL-3": 3, "SEAL-4": 4}
        return [patch.object(OL.P, "connected_provider_ids", lambda: set(connected)),
                patch.object(OL.P, "list_paused_providers", lambda: list(paused)),
                patch.object(OL.P, "provider_meets_tier",
                             lambda p, t: rank[seal[p]] >= rank[t])]

    def _pick(self, tier, connected, paused=()):
        ps = self._providers(connected, paused)
        for p in ps:
            p.start()
        try:
            return OL.pick_provider(tier)
        finally:
            for p in ps:
                p.stop()

    def test_the_tier_is_respected_and_subscriptions_are_never_used(self):
        every = ["scaleway", "aws-region-eu", "openai-api", "claude-team"]
        self.assertEqual(self._pick("SEAL-1", every), "scaleway")      # cheapest
        self.assertEqual(self._pick("SEAL-2", ["aws-region-eu", "openai-api"]),
                         "aws-region-eu")
        with self.assertRaises(OL.NoEligibleProvider):
            self._pick("SEAL-2", ["openai-api", "claude-team"])
        with self.assertRaises(OL.NoEligibleProvider):
            self._pick("SEAL-1", ["claude-team"])                     # subscription only
        self.assertEqual(self._pick("SEAL-1", every, paused=["scaleway"]), "openai-api")

    def test_the_request_carries_no_tools(self):
        sent = {}

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": " ok "}}]}

        class Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                sent.update(url=url, body=json)
                return Resp()
        ps = self._providers(["scaleway"]) + [
            patch.object(OL.P, "_read", lambda p: {"api_key": "k", "method": "apikey"}),
            patch.object(OL.httpx, "Client", Client)]
        for p in ps:
            p.start()
        try:
            out, pid = OL.complete("SEAL-3", "sys", "testo")
        finally:
            for p in ps:
                p.stop()
        self.assertEqual((out, pid), ("ok", "scaleway"))
        self.assertTrue(sent["url"].endswith("/chat/completions"))
        self.assertNotIn("tools", sent["body"])
        self.assertNotIn("tool_choice", sent["body"])
        self.assertEqual([m["role"] for m in sent["body"]["messages"]], ["system", "user"])


class SwitchTests(_Base):
    def test_default_on_and_retier(self):
        self.assertTrue(MS.get("SEAL-1", "ops")["enabled"])
        MS.set_enabled("SEAL-1", "ops", False, by="davide")
        self.assertTrue(MS.retier("SEAL-1", "SEAL-2", "ops"))
        self.assertFalse(MS.get("SEAL-2", "ops")["enabled"])
        self.assertTrue(MS.get("SEAL-1", "ops")["enabled"])     # nothing left behind

    def test_routes_owner_writes_participant_reads(self):
        from fastapi import HTTPException

        from ..scheduler import api
        topic = {"meta": {"owner": "davide", "participants": ["davide", "mario"]}}

        def run(coro, who):
            with patch.object(api, "_caller", return_value=who), \
                    patch.object(api.topics_client, "open_topic", return_value=topic), \
                    patch("server.api.admin.is_admin", lambda p: False):
                return asyncio.run(coro)
        self.assertTrue(run(api.api_get_topic_mail_summary("SEAL-1", "ops", None),
                            "mario")["summary"]["enabled"])
        out = run(api.api_put_topic_mail_summary(
            "SEAL-1", "ops", api.TopicMailSummarySet(enabled=False), None), "davide")
        self.assertFalse(out["summary"]["enabled"])
        with self.assertRaises(HTTPException):
            run(api.api_put_topic_mail_summary(
                "SEAL-1", "ops", api.TopicMailSummarySet(enabled=True), None), "mario")


if __name__ == "__main__":
    unittest.main()
