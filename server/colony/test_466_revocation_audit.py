"""clodia-platform#466: a change of `pki/revoked.json` is a control-plane
change, and it reaches the audit trail — or it says loudly that it did not.

The invariant under test: `revoked.json` never changes without a
`control.pki` event, either confirmed by the gateway or waiting in the outbox
with an error raised (CLI) or logged (server).
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from . import pki
from .test_pki import PkiBase


class RevocationAuditTests(PkiBase):
    def setUp(self) -> None:
        super().setUp()
        pki.init_ca()
        pki.issue_agent_identity("minerva")
        self.audit.clear()          # issuance is the gateway's to record, not ours

    def _revoked_json(self) -> str | None:
        return pki.REVOKED_FILE.read_text() if pki.REVOKED_FILE.is_file() else None

    def pki_events(self) -> list[dict]:
        return [e for e in self.audit if e["type"] == "control.pki"]

    def test_revoke_records_principal_actor_and_serial(self) -> None:
        from cryptography import x509
        serial = format(x509.load_pem_x509_certificate(
            pki.agent_cert_path("minerva").read_bytes()).serial_number, "x")
        self.assertTrue(pki.revoke("minerva", actor={"type": "human", "id": "davide"}))
        (ev,) = self.pki_events()
        self.assertEqual((ev["action"], ev["resource"]), ("revoke", "minerva"))
        self.assertEqual(ev["actor"], {"type": "human", "id": "davide"})
        self.assertEqual(ev["result"]["principal"], "minerva")
        self.assertEqual(ev["result"]["cert_serial"], serial)

    def test_revoked_json_never_changes_without_the_event(self) -> None:
        """The acceptance of #466: a change of the file with no event is a failure."""
        before = self._revoked_json()
        pki.revoke("minerva")
        self.assertNotEqual(self._revoked_json(), before)
        self.assertEqual([e["action"] for e in self.pki_events()], ["revoke"])
        # Revoking again changes nothing, and records nothing.
        before = self._revoked_json()
        self.assertFalse(pki.revoke("minerva"))
        self.assertEqual(self._revoked_json(), before)
        self.assertEqual(len(self.pki_events()), 1)

    def test_clearing_on_reissue_is_an_unrevoke_event(self) -> None:
        pki.revoke("minerva")
        pki.issue_agent_identity("minerva", force=True)
        self.assertFalse(pki.is_revoked("minerva"))
        acts = [(e["action"], e["resource"]) for e in self.pki_events()]
        self.assertEqual(acts, [("revoke", "minerva"), ("unrevoke", "minerva")])
        unrev = self.pki_events()[1]
        # The serial is the NEW certificate's: the one that re-enables it.
        self.assertEqual(unrev["result"]["cert_serial"], pki._cert_serial("minerva"))
        self.assertEqual(unrev["result"]["via"], "issue_agent_identity")
        self.assertEqual(unrev["actor"], {"type": "service", "id": "agent-server"})

    def test_clearing_for_an_external_key_principal_is_recorded_too(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        pem = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        pki.issue_cert_for_pubkey("davide", pem)
        pki.revoke("davide")
        pki.issue_cert_for_pubkey("davide", pem, force=True)
        self.assertEqual([e["action"] for e in self.pki_events()], ["revoke", "unrevoke"])
        self.assertEqual(self.pki_events()[1]["result"]["via"], "issue_cert_for_pubkey")

    def test_a_trail_that_does_not_answer_makes_revoke_fail_loudly(self) -> None:
        self.audit_up = False
        with self.assertRaises(pki.RevocationNotRecorded):
            pki.revoke("minerva")
        # Security first: the revocation IS in effect …
        self.assertTrue(pki.is_revoked("minerva"))
        # … and its event is not lost: it waits in the outbox.
        (queued,) = pki.pending_audit_events()
        self.assertEqual((queued["action"], queued["resource"]), ("revoke", "minerva"))
        self.assertEqual(os.stat(pki._outbox_path()).st_mode & 0o777, 0o600)

    def test_the_outbox_is_delivered_in_order_when_the_trail_is_back(self) -> None:
        self.audit_up = False
        with self.assertRaises(pki.RevocationNotRecorded):
            pki.revoke("minerva")
        pki.issue_agent_identity("minerva", force=True)   # server context: logged, queued
        self.assertEqual([e["action"] for e in pki.pending_audit_events()],
                         ["revoke", "unrevoke"])
        self.audit_up = True
        self.assertEqual(pki.flush_audit_outbox(), 2)
        self.assertEqual([e["action"] for e in self.pki_events()], ["revoke", "unrevoke"])
        self.assertTrue(all(e["result"]["recorded_late"] for e in self.pki_events()))
        self.assertEqual(pki.pending_audit_events(), [])
        self.assertFalse(pki._outbox_path().exists())

    def test_a_new_event_does_not_overtake_a_queued_one(self) -> None:
        self.audit_up = False
        with self.assertRaises(pki.RevocationNotRecorded):
            pki.revoke("minerva")
        self.audit_up = True
        pki.issue_agent_identity("satia")
        pki.revoke("satia")          # flushes the queue first, then records its own
        self.assertEqual([(e["action"], e["resource"]) for e in self.pki_events()],
                         [("revoke", "minerva"), ("revoke", "satia")])

    def test_the_cli_actor_is_who_ran_it(self) -> None:
        with patch.object(pki, "_CLI_ACTOR", {"type": "human", "id": "davide",
                                              "via": "pki-cli", "os_user": "root"}):
            pki.revoke("minerva")
            self.audit_up = False
            # From the CLI a re-issue whose unrevoke is not recorded is loud too.
            with self.assertRaises(pki.RevocationNotRecorded):
                pki.issue_agent_identity("minerva", force=True)
        self.assertEqual(self.pki_events()[0]["actor"]["id"], "davide")
        self.assertEqual(self.pki_events()[0]["actor"]["via"], "pki-cli")


class OutboxRobustnessTests(PkiBase):
    """Review of #491: concurrent writers, re-sends, file mode."""

    def _event(self, i: int) -> dict:
        return pki._revocation_event("revoke", f"p{i}", actor=None, via="test")

    def test_interleaved_enqueue_and_flush_lose_no_event(self) -> None:
        import threading
        import random
        delivered: list[str] = []
        lock = threading.Lock()

        def flaky(ev):
            # Up half of the time, and slow: a flush that rewrites the file
            # while writers keep appending is exactly the race.
            import time
            time.sleep(random.random() / 2000)
            if random.random() < 0.5:
                return False
            with lock:
                delivered.append(ev["event_id"])
            return True
        queued: list[str] = []

        def writer(base):
            for i in range(40):
                ev = self._event(base + i)
                with lock:
                    queued.append(ev["event_id"])
                pki._outbox_append(ev)

        def flusher():
            for _ in range(60):
                pki.flush_audit_outbox()
        with patch("server.audit_events.report_sync", flaky):
            ts = [threading.Thread(target=writer, args=(b,)) for b in (0, 100, 200)]
            ts += [threading.Thread(target=flusher) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        left = [e["event_id"] for e in pki.pending_audit_events()]
        self.assertEqual(sorted(delivered + left), sorted(queued))   # none lost …
        self.assertEqual(len(set(delivered)), len(delivered))        # … none sent twice

    def test_a_resent_event_keeps_its_event_id(self) -> None:
        self.audit_up = False
        with self.assertRaises(pki.RevocationNotRecorded):
            pki.revoke("minerva")
        (queued,) = pki.pending_audit_events()
        self.assertRegex(queued["event_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(queued["result"]["source_event_id"], queued["event_id"])
        self.audit_up = True
        pki.flush_audit_outbox()
        self.assertEqual(self.audit[-1]["event_id"], queued["event_id"])

    def test_the_outbox_is_born_0600_whatever_the_umask(self) -> None:
        old = os.umask(0)
        try:
            pki._outbox_append(self._event(1))
        finally:
            os.umask(old)
        self.assertEqual(os.stat(pki._outbox_path()).st_mode & 0o777, 0o600)
        import inspect
        self.assertNotIn("chmod", inspect.getsource(pki._outbox_append))


class ReportSyncTests(unittest.TestCase):
    """The synchronous channel the CLI uses is the same one the turns use."""

    def test_without_the_orchestrator_secret_nothing_is_recorded(self) -> None:
        from .. import audit_events
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": ""}):
            self.assertFalse(audit_events.report_sync({"type": "control.pki"}))

    def test_it_posts_to_the_gateway_with_the_secret(self) -> None:
        from .. import audit_events
        seen = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"recorded": True, "event_id": "e1"}

        class _Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, headers=None):
                seen.update(url=url, body=json, headers=headers)
                return _Resp()

        import httpx
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "s3",
                                     "CLODIA_TOOLS_MCP_URL": "http://gw:7849/mcp/"}), \
                patch.object(httpx, "Client", _Client):
            ok = audit_events.report_sync({"type": "control.pki", "action": "revoke",
                                           "resource": "minerva", "scope": None})
        self.assertTrue(ok)
        self.assertEqual(seen["url"], "http://gw:7849/internal/audit/event")
        self.assertEqual(seen["headers"], {"X-Orchestrator-Secret": "s3"})
        self.assertNotIn("scope", seen["body"])        # pruned like `report`
        json.dumps(seen["body"])


if __name__ == "__main__":
    unittest.main()
