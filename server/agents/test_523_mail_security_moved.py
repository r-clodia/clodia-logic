"""comms-pack's mail component — the security tests of the gateway's native
mail client, moved here (clodia-platform#523).

clodia-tools removes `server/tools/email.py` and `vendor/email_client.py`:
comms-pack's gateway component is the only mail implementation left, and the
cases those tests protected now have to hold HERE. Ported, on the fake host of
`test_558_mail_component.py`, only what that file did not cover already:

- #400 — `message/rfc822` (.eml) attachments are retrievable through the
  component's `email_client.py` (their payload is a sub-message, not bytes);
- #461 — an agent's attachment path is confined to ITS scratch: outside,
  through a symlink, relative, another spawn's scratch → refused, for `send`
  and `reply`; what is sent is the resolved path;
- #420 — an attachment name is one file name, never a path;
- #503 — strict ingress where it is the component's: `search`, unparseable
  senders, attachments and replies refused before anything is fetched, no
  extra fetch on a non-strict channel;
- #428 — `send` and `reply` are weighed on the OUTBOX whitelist;
- #556/#560 — TLS verifies the server; a non-ASCII attachment name survives;
- the account rules of `email.*` and the "accounts not allowed" note.
"""
from __future__ import annotations

import base64
import contextlib
import email as email_mod
import importlib.util
import os
import ssl
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

from .test_558_mail_component import CLIENT, STUDIO, _OnFakeHost


def _client():
    """The component's own `email_client.py`, loaded without bytecode."""
    spec = importlib.util.spec_from_file_location("comms_pack_email_client_523", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = before
    return mod


# ── #400: message/rfc822 attachments ───────────────────────────────────────

PDF_BYTES = b"%PDF-1.4 fake but binary \x00\x01\x02"
EML_NAME = "HikmaAI Docs.eml"
PDF_NAME = "condizioni-generali.pdf"


def _message_with_eml_attachment() -> EmailMessage:
    inner = EmailMessage()
    inner["From"] = "mittente@example.com"
    inner["To"] = "info@example.com"
    inner["Subject"] = "Documento inoltrato"
    inner.set_content("nested body")
    msg = EmailMessage()
    msg["From"] = "info@example.com"
    msg["To"] = "owner@example.com"
    msg["Subject"] = "batch"
    msg.set_content("an .eml and a pdf attached")
    msg.add_attachment(PDF_BYTES, maintype="application", subtype="pdf", filename=PDF_NAME)
    msg.add_attachment(inner, filename=EML_NAME)
    return msg


class _FakeIMAP:
    def __init__(self, raw: bytes):
        self._raw = raw

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def fetch(self, email_id, parts):
        return "OK", [(b"1 (RFC822 {%d}" % len(self._raw), self._raw)]

    def logout(self):
        return "BYE", [b""]


class Rfc822AttachmentTests(unittest.TestCase):
    """#400: `get_payload(decode=True)` is None on a message/rfc822 part; the
    client used to skip it and answer «not found» for every .eml."""

    def setUp(self) -> None:
        self.c = _client()
        self.raw = _message_with_eml_attachment().as_bytes()
        p = patch.object(self.c, "connect_imap", return_value=_FakeIMAP(self.raw))
        p.start()
        self.addCleanup(p.stop)

    def _part(self, filename):
        for part in email_mod.message_from_bytes(self.raw).walk():
            if part.get_filename() == filename:
                return part
        raise AssertionError(filename)

    def _expected_eml(self) -> bytes:
        return self._part(EML_NAME).get_payload(decode=False)[0].as_bytes()

    def test_the_fixture_reproduces_the_none_payload(self) -> None:
        self.assertIsNone(self._part(EML_NAME).get_payload(decode=True))

    def test_get_attachment_returns_the_eml_bytes(self) -> None:
        got = self.c.get_attachment("info", "177", EML_NAME)
        data = base64.b64decode(got["data"])
        self.assertEqual(got["content_type"], "message/rfc822")
        self.assertEqual(data, self._expected_eml())
        self.assertEqual(email_mod.message_from_bytes(data)["Subject"], "Documento inoltrato")

    def test_binaries_still_work_and_absent_still_raises(self) -> None:
        got = self.c.get_attachment("info", "177", PDF_NAME)
        self.assertEqual(base64.b64decode(got["data"]), PDF_BYTES)
        with self.assertRaises(ValueError):
            self.c.get_attachment("info", "177", "missing.txt")

    def test_download_attachments_writes_the_eml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.c.download_attachments("info", "177", output_dir=tmp)
            self.assertEqual(Path(tmp, EML_NAME).read_bytes(), self._expected_eml())
            self.assertEqual(Path(tmp, PDF_NAME).read_bytes(), PDF_BYTES)

    def test_part_bytes(self) -> None:
        part = email_mod.message_from_string("Content-Type: message/rfc822\n\n")
        self.assertTrue(self.c._part_bytes(part))
        part.set_payload([])
        self.assertIsNone(self.c._part_bytes(part))
        b64 = email_mod.message_from_string(
            "Content-Type: application/octet-stream\n"
            "Content-Transfer-Encoding: base64\n\nY2lhbw==\n")
        self.assertEqual(self.c._part_bytes(b64), b"ciao")


# ── #556/#560: TLS and attachment names in the client ──────────────────────

class ClientTlsAndNamesTests(unittest.TestCase):
    def test_imap_and_smtp_verify_the_server(self) -> None:
        c = _client()
        seen = {}

        def imap(host, port, ssl_context=None):
            seen["imap"] = ssl_context
            return MagicMock()

        smtp = MagicMock()
        smtp.return_value.starttls.side_effect = (
            lambda context=None: seen.setdefault("smtp", context))
        acct = {"imap_server": "imap.x", "imap_port": 993, "smtp_server": "smtp.x",
                "smtp_port": 587, "email": "a@x", "password": "p"}
        with patch.object(c.imaplib, "IMAP4_SSL", side_effect=imap), \
                patch.object(c.smtplib, "SMTP", smtp):
            c.connect_imap(acct)
            c.connect_smtp(acct)
        for k in ("imap", "smtp"):
            self.assertIsInstance(seen[k], ssl.SSLContext, k)
            self.assertEqual(seen[k].verify_mode, ssl.CERT_REQUIRED, k)
            self.assertTrue(seen[k].check_hostname, k)

    def test_a_non_ascii_attachment_name_survives(self) -> None:
        c = _client()
        sent = {}

        class S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def send_message(self, msg):
                sent["raw"] = msg.as_bytes()

        with tempfile.TemporaryDirectory() as td:
            att = Path(td) / "offerta àè.pdf"
            att.write_bytes(b"%PDF-1.4")
            acct = {"email": "a@x", "smtp_server": "s", "smtp_port": 587}
            with patch.object(c, "connect_smtp", return_value=S()):
                c.send_email(acct, "b@y", "s", "body", attachments=[str(att)])
        msg = email_mod.message_from_bytes(sent["raw"])
        self.assertEqual([p.get_filename() for p in msg.walk() if p.get_filename()],
                         ["offerta àè.pdf"])


# ── #420: an attachment name is not a path ─────────────────────────────────

class SafeAttachmentNameTests(_OnFakeHost):
    LOST = ["Aggiornamento preventivo Voucher Startup Social Tech / StopListe.eml",
            "NDA bilaterale Tomato / Luxury Esmeralda Real Estate — versione aggiornata.eml"]

    def test_the_separator_stops_being_a_separator(self) -> None:
        for name in self.LOST:
            safe = self.mail.safe_attachment_name(name)
            self.assertNotIn("/", safe)
            self.assertTrue(safe.endswith(".eml"), safe)
            self.assertEqual(safe, safe.strip())

    def test_backslash_newline_and_leading_dot(self) -> None:
        f = self.mail.safe_attachment_name
        self.assertEqual(f("a\\b.pdf"), "a-b.pdf")
        self.assertEqual(f("Oggetto\n lungo.eml"), "Oggetto lungo.eml")
        self.assertEqual(f(".hidden.pdf"), "hidden.pdf")

    def test_a_name_that_sanitizes_to_nothing_still_has_a_name(self) -> None:
        self.assertEqual(self.mail.safe_attachment_name("///"), "allegato")
        self.assertEqual(self.mail.safe_attachment_name(""), "allegato")


# ── #461: an agent's attachments stay in its own scratch ───────────────────

class AttachmentsConfinedTests(_OnFakeHost):
    def setUp(self) -> None:
        super().setUp()
        self.mine = Path(self.tmp) / "spawns" / "clodia-320"
        self.other = Path(self.tmp) / "spawns" / "ophelia-7"
        self.mine.mkdir(parents=True)
        self.other.mkdir(parents=True)
        self.host.scratch = str(self.mine)
        self.sent: list = []
        capture = lambda *a, **k: self.sent.append(k.get("attachments")) or {"ok": True}
        for p in (patch.object(self.mail, "send", capture),
                  patch.object(self.mail, "reply", capture)):
            p.start()
            self.addCleanup(p.stop)

    def _call(self, verb, attachments):
        args = {"account": "studio", "body": "b", "attachments": attachments}
        if verb == "email.send":
            args.update(to="x@example.com", subject="s")
        else:
            args.update(email_id="42")
        return self.call(verb, args)

    def _refused(self, verb, path) -> None:
        with self.assertRaises(ValueError) as e:
            self._call(verb, [path])
        self.assertIn("outside your scratch", str(e.exception))

    def test_a_gateway_file_outside_the_scratch_is_refused(self) -> None:
        secret = Path(self.tmp) / "data" / "config.yaml"
        secret.parent.mkdir()
        secret.write_text("vault: x\n")
        for verb in ("email.send", "email.reply"):
            self._refused(verb, str(secret))
        self.assertEqual(self.sent, [])

    def test_a_symlink_out_of_the_scratch_is_refused(self) -> None:
        target = Path(self.tmp) / "outside.txt"
        target.write_text("x")
        link = self.mine / "looks-local.txt"
        os.symlink(target, link)
        self._refused("email.send", str(link))
        self.assertEqual(self.sent, [])

    def test_a_relative_path_is_refused(self) -> None:
        self._refused("email.send", "report.pdf")
        self.assertEqual(self.sent, [])

    def test_another_spawns_scratch_is_refused(self) -> None:
        theirs = self.other / "theirs.pdf"
        theirs.write_bytes(b"x")
        self._refused("email.send", str(theirs))
        self.assertEqual(self.sent, [])

    def test_a_file_in_the_scratch_is_sent_by_its_resolved_path(self) -> None:
        att = self.mine / "offerta.pdf"
        att.write_bytes(b"%PDF")
        for verb in ("email.send", "email.reply"):
            self._call(verb, [str(att)])
        self.assertEqual(self.sent, [[os.path.realpath(att)], [os.path.realpath(att)]])

    def test_a_missing_file_in_the_scratch_is_refused(self) -> None:
        with self.assertRaises(ValueError) as e:
            self._call("email.send", [str(self.mine / "nope.pdf")])
        self.assertIn("not found", str(e.exception))
        self.assertEqual(self.sent, [])


# ── #503: strict ingress, the cases test_558 did not cover ─────────────────

class StrictIngressMoreTests(_OnFakeHost):
    ROWS = [{"id": "1", "from": "Fornitore <ordini@f.it>", "subject": "ok"},
            {"id": "2", "from": "spam@x.it", "subject": "offerta riservata"},
            {"id": "4", "from": "non-un-indirizzo", "subject": "?"}]

    def setUp(self) -> None:
        super().setUp()
        self.host.perimeter.strict = "SEAL-1/acme"
        self.host.perimeter.vetted.add("mailfrom:ordini@f.it")

    def _cli(self, sender):
        return patch.object(self.mail, "_run_cli",
                            return_value={"id": "9", "from": sender, "body": "x"})

    def test_search_is_filtered_the_same_way(self) -> None:
        with patch.object(self.mail, "_run_json", return_value=list(self.ROWS)):
            out = self.call("email.search", {"account": "studio", "query": "ALL"})
        self.assertEqual([m["id"] for m in out["results"]], ["1"])
        self.assertEqual(out["withheld"], 2)
        self.assertNotIn("offerta riservata", repr(out))

    def test_an_unparseable_sender_is_withheld_and_denied(self) -> None:
        with patch.object(self.mail, "_run_json", return_value=[self.ROWS[2]]):
            out = self.call("email.list", {"account": "studio"})
        self.assertEqual((out["messages"], out["withheld"]), ([], 1))
        with patch.object(self.mail, "_run_json", return_value={"id": "4", "from": "???"}):
            with self.assertRaises(PermissionError):
                self.call("email.read", {"account": "studio", "email_id": "4"})

    def test_attachments_of_a_stranger_are_denied_before_the_bytes(self) -> None:
        self.host.topic = ("SEAL-1", "acme")          # save_attachment lands in a topic
        self.host.members.add(("SEAL-1", "acme"))
        for verb in ("email.get_attachment", "email.save_attachment"):
            with self.subTest(verb=verb), self._cli("spam@x.it"), \
                    patch.object(self.mail, "_run_json") as run_json:
                with self.assertRaises(PermissionError):
                    self.call(verb, {"account": "studio", "email_id": "9",
                                     "filename": "fattura.pdf"})
                run_json.assert_not_called()
        self.assertEqual(self.host.topics.files, {})

    def test_a_vetted_senders_attachment_passes(self) -> None:
        with self._cli("ordini@f.it"), \
                patch.object(self.mail, "_run_json", return_value={"data": "eA=="}) as rj:
            self.call("email.get_attachment", {"account": "studio", "email_id": "9",
                                               "filename": "fattura.pdf"})
        rj.assert_called_once()

    def test_reply_to_a_stranger_is_denied_and_weighs_the_outbox(self) -> None:
        with self._cli("spam@x.it"), patch.object(self.mail, "_run_json") as rj:
            with self.assertRaises(PermissionError):
                self.call("email.reply", {"account": "studio", "email_id": "9",
                                          "body": "ok"})
        rj.assert_not_called()
        self.assertEqual(self.host.decisions[-1][:3], ("email.reply", "ingress", "deny"))
        with self._cli("ordini@f.it") as cli, \
                patch.object(self.mail, "_run_json", return_value={"status": "ok"}):
            self.call("email.reply", {"account": "studio", "email_id": "9", "body": "ok"})
        self.assertEqual(cli.call_args.kwargs["direction"], "outbox")

    def test_a_non_strict_channel_costs_no_extra_fetch(self) -> None:
        self.host.perimeter.strict = None
        with patch.object(self.mail, "_run_cli") as cli, \
                patch.object(self.mail, "_run_json", return_value={"data": "eA=="}):
            self.call("email.get_attachment", {"account": "studio", "email_id": "9",
                                               "filename": "f.pdf"})
        cli.assert_not_called()


# ── #428: the way out is weighed on the OUTBOX whitelist ───────────────────

class SendDirectionTests(_OnFakeHost):
    def _directions(self, verb, args):
        seen = []

        @contextlib.contextmanager
        def env(account, direction):
            seen.append(direction)
            yield {}

        done = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        with patch.object(self.mail, "_secrets_env", env), \
                patch.object(self.mail.subprocess, "run", return_value=done) as run:
            self.call(verb, args)
        return seen, run

    def test_send_is_checked_on_the_outbox(self) -> None:
        seen, run = self._directions("email.send", {
            "account": "studio", "to": "a@b.it", "subject": "s", "body": "t",
            "cc": "c@b.it"})
        self.assertEqual(seen, ["outbox"])
        cmd = run.call_args[0][0]
        self.assertIn("send", cmd)
        self.assertIn("c@b.it", cmd)

    def test_reply_is_checked_on_the_outbox_too(self) -> None:
        seen, _ = self._directions("email.reply", {"account": "studio", "email_id": "42",
                                                   "body": "grazie"})
        self.assertEqual(seen, ["outbox"])

    def test_without_the_outbox_entry_nothing_is_sent(self) -> None:
        self.host.perimeter.outbox.clear()
        with patch.object(self.mail.subprocess, "run") as run:
            with self.assertRaises(PermissionError) as e:
                self.call("email.send", {"account": "studio", "to": "a@b.it",
                                         "subject": "s", "body": "t"})
        run.assert_not_called()
        self.assertIn("outbox:studio@example.it", str(e.exception))


# ── accounts: which one, and the ones this room cannot read ────────────────

class AccountRulesTests(_OnFakeHost):
    def test_a_single_account_is_selected(self) -> None:
        with patch.object(self.mail, "_run_json", return_value=[]) as rj:
            self.call("email.list", {})
        self.assertEqual(rj.call_args.args[0], "studio")

    def test_an_unknown_account_names_the_valid_ones(self) -> None:
        with self.assertRaises(ValueError) as e:
            self.call("email.list", {"account": "demo"})
        self.assertIn("'demo' non esistente", str(e.exception))
        self.assertIn("studio", str(e.exception))

    def test_an_existing_account_outside_the_inbox_list_is_named(self) -> None:
        self.host.vault["mailbox_team"] = dict(STUDIO, email="team@x.it")
        self.host.vault["mailbox_rotta"] = {"email": "rotta@x.it", "smtp_server": "s",
                                            "smtp_port": 1}       # no password
        with patch.object(self.mail, "_run_json", return_value=["INBOX"]):
            out = self.call("email.folders", {"account": "studio"})
        self.assertEqual(out["accounts_not_allowed"], ["team"])      # not the broken one
        self.assertIn("inbox:", out["note"])
        self.host.perimeter.inbox.add("team@x.it")
        with patch.object(self.mail, "_run_json", return_value=["INBOX"]):
            out = self.call("email.folders", {"account": "studio"})
        self.assertNotIn("accounts_not_allowed", out)
        self.assertNotIn("note", out)


if __name__ == "__main__":
    unittest.main()
