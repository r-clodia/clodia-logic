"""#585 / #586 — comms-pack side of the courier.

#585: ONE search per cycle lists which message-box codes have recent mail
(`courier_box_index`, keys only); per-room reads take `only` those keys.
#586: `courier_eml` says whether the receiving server authenticated the sender
(topmost `Authentication-Results`, trusted authserv-id, DMARC or aligned DKIM)."""
from __future__ import annotations

from unittest.mock import patch

from .test_558_mail_component import CourierTests, _OnFakeHost

GMAIL_AR = ("mx.google.com; dkim=pass header.i=@gmail.com header.s=20251104 "
            "header.b=eGe3ouF4; arc=pass (i=1); spf=pass (google.com: domain of "
            "a@gmail.com designates 1.2.3.4 as permitted sender) smtp.mailfrom=a@gmail.com; "
            "dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com")


def _raw(*headers: str, body: str = "ciao") -> bytes:
    return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode()


class _Base(_OnFakeHost):
    LIST = CourierTests.LIST
    _fake = CourierTests._fake
    svc = CourierTests.svc

    def setUp(self) -> None:
        super().setUp()
        self.host.perimeter.room_senders = {"ordini@f.it", "spam@x.it"}


class BoxIndexTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.host.perimeter.colony = "studio@example.it"
        self.host.perimeter.tag = "*"
        self.searches = []

    def fake(self, account, args, **kw):
        if args[0] == "search":
            self.searches.append(args[1])
            return [dict(r) for r in self.LIST]
        return self._fake(account, args)

    def test_one_search_gives_every_code_with_its_keys(self) -> None:
        with patch.object(self.mail, "_run_json", side_effect=self.fake):
            out = self.svc("courier_box_index", {"account": "studio"})
        self.assertEqual(out["tags"], {"DJ56K": [self.mail.envelope_key(self.LIST[0])]})
        (query,) = self.searches
        self.assertTrue(query.startswith("SINCE "))

    def test_keys_only_nothing_else(self) -> None:
        with patch.object(self.mail, "_run_json", side_effect=self.fake):
            out = self.svc("courier_box_index", {"account": "studio"})
        self.assertNotIn("fattura", repr(out))
        self.assertNotIn("ordini@f.it", repr(out))

    def test_only_under_the_index_tag(self) -> None:
        for tag in ("", "DJ56K"):
            with self.subTest(tag=tag):
                self.host.perimeter.tag = tag
                with patch.object(self.mail, "_run_json", side_effect=self.fake):
                    with self.assertRaises(PermissionError):
                        self.svc("courier_box_index", {"account": "studio"})

    def test_only_on_the_colony_mailbox(self) -> None:
        self.host.perimeter.colony = "altro@example.it"
        with patch.object(self.mail, "_run_json", side_effect=self.fake):
            with self.assertRaises(PermissionError):
                self.svc("courier_box_index", {"account": "studio"})

    def test_the_index_tag_opens_no_per_room_read(self) -> None:
        with patch.object(self.mail, "_run_json", side_effect=self.fake):
            for name, args in (("courier_envelopes", {"account": "studio"}),
                               ("courier_eml", {"account": "studio", "email_id": "1"}),
                               ("courier_attachment", {"account": "studio", "email_id": "1",
                                                       "filename": "a/b.pdf"})):
                with self.subTest(service=name):
                    with self.assertRaises(PermissionError):
                        self.svc(name, args)


class OnlyTests(_Base):
    def test_only_the_named_keys_are_read(self) -> None:
        k2 = self.mail.envelope_key(self.LIST[1])
        with patch.object(self.mail, "_run_json", side_effect=self._fake):
            out = self.svc("courier_envelopes", {"account": "studio", "only": [k2]})
            none = self.svc("courier_envelopes", {"account": "studio", "only": []})
            every = self.svc("courier_envelopes", {"account": "studio"})
        self.assertEqual([e["id"] for e in out["envelopes"]], ["2"])
        self.assertEqual(none["envelopes"], [])
        self.assertEqual([e["id"] for e in every["envelopes"]], ["1", "2"])

    def test_the_eml_carries_the_authentication_result(self) -> None:
        with patch.object(self.mail, "_run_json", side_effect=self._fake):
            out = self.svc("courier_eml", {"account": "studio", "email_id": "1"})
        self.assertIs(out["auth"]["authenticated"], False)   # RAW-EML-1: no headers


class AuthenticationTests(_Base):
    IDS = {"mx.google.com"}

    def auth(self, raw, ids=None):
        return self.mail.mail_authentication(raw, self.IDS if ids is None else ids)

    def test_gmail_dmarc_pass_is_authenticated(self) -> None:
        out = self.auth(_raw(f"Authentication-Results: {GMAIL_AR}",
                             "From: Davide <a@gmail.com>"))
        self.assertEqual((out["authenticated"], out["method"], out["domain"]),
                         (True, "dmarc", "gmail.com"))

    def test_aligned_dkim_alone_is_enough(self) -> None:
        ar = "mx.google.com; dkim=pass header.i=@mail.example.it header.s=s1; spf=none"
        out = self.auth(_raw(f"Authentication-Results: {ar}", "From: x@example.it"))
        self.assertEqual((out["authenticated"], out["method"]), (True, "dkim"))

    def test_unaligned_dkim_is_not(self) -> None:
        ar = "mx.google.com; dkim=pass header.d=mailer.com; dmarc=fail header.from=example.it"
        self.assertFalse(self.auth(_raw(f"Authentication-Results: {ar}",
                                        "From: x@example.it"))["authenticated"])

    def test_dmarc_for_another_domain_is_not(self) -> None:
        ar = "mx.google.com; dmarc=pass header.from=evil.com"
        self.assertFalse(self.auth(_raw(f"Authentication-Results: {ar}",
                                        "From: a@gmail.com"))["authenticated"])

    def test_a_forged_header_below_the_receivers_does_not_count(self) -> None:
        receiver = "mx.google.com; dkim=fail header.i=@gmail.com; dmarc=fail header.from=gmail.com"
        out = self.auth(_raw(f"Authentication-Results: {receiver}",
                             f"Authentication-Results: {GMAIL_AR}",
                             "From: a@gmail.com"))
        self.assertFalse(out["authenticated"])

    def test_an_untrusted_authserv_id_does_not_count(self) -> None:
        out = self.auth(_raw("Authentication-Results: evil.example; dmarc=pass header.from=gmail.com",
                             "From: a@gmail.com"))
        self.assertFalse(out["authenticated"])
        self.assertIn("evil.example", out["reason"])

    def test_no_trusted_id_nothing_authenticated(self) -> None:
        self.assertFalse(self.auth(_raw(f"Authentication-Results: {GMAIL_AR}",
                                        "From: a@gmail.com"), ids=set())["authenticated"])

    def test_from_must_be_one_address(self) -> None:
        for frm in ("From: a@gmail.com, b@gmail.com", "From: nessuno"):
            with self.subTest(frm=frm):
                self.assertFalse(self.auth(_raw(f"Authentication-Results: {GMAIL_AR}",
                                                frm))["authenticated"])
        two = _raw(f"Authentication-Results: {GMAIL_AR}", "From: a@gmail.com",
                   "From: b@evil.com")
        self.assertFalse(self.auth(two)["authenticated"])

    def test_no_header_no_authentication(self) -> None:
        out = self.auth(_raw("From: a@gmail.com"))
        self.assertEqual((out["authenticated"], out["reason"]),
                         (False, "no Authentication-Results"))

    def test_trusted_ids_follow_the_mailbox_kind(self) -> None:
        self.assertEqual(self.mail._authserv_ids("studio"), set())
        self.host.vault["mailbox_studio"] = {**self.host.vault["mailbox_studio"],
                                             "authserv_id": "mx.example.it"}
        self.assertEqual(self.mail._authserv_ids("studio"), {"mx.example.it"})


class ReviewFixesTests(_Base):
    """Review of clodia-logic#558."""

    def test_refused_keys_are_reported_for_an_only_pass(self) -> None:
        self.host.perimeter.room_senders = {"ordini@f.it"}
        k1, k2 = (self.mail.envelope_key(r) for r in self.LIST)
        with patch.object(self.mail, "_run_json", side_effect=self._fake):
            out = self.svc("courier_envelopes", {"account": "studio", "only": [k1, k2]})
            plain = self.svc("courier_envelopes", {"account": "studio"})
        self.assertEqual([e["id"] for e in out["envelopes"]], ["1"])
        self.assertEqual(out["withheld_keys"], [k2])
        self.assertNotIn("withheld_keys", plain)

    def test_an_unreachable_message_is_not_reported_refused(self) -> None:
        k1 = self.mail.envelope_key(self.LIST[0])

        def fake(account, args, **kw):
            if args[0] == "read":
                return None                     # the read failed
            return self._fake(account, args)
        with patch.object(self.mail, "_run_json", side_effect=fake):
            out = self.svc("courier_envelopes", {"account": "studio", "only": [k1]})
        self.assertEqual((out["envelopes"], out["withheld_keys"]), ([], []))

    def test_the_eml_of_another_message_is_refused(self) -> None:
        with patch.object(self.mail, "_run_json", side_effect=self._fake):
            with self.assertRaises(PermissionError):
                self.svc("courier_eml", {"account": "studio", "email_id": "1",
                                         "key": self.mail.envelope_key(self.LIST[1])})
            ok = self.svc("courier_eml", {"account": "studio", "email_id": "1",
                                          "key": self.mail.envelope_key(self.LIST[0])})
        self.assertEqual(ok["size"], 9)

    def test_an_eml_without_message_id_is_never_authenticated(self) -> None:
        raw = _raw(f"Authentication-Results: {GMAIL_AR}", "From: ordini@f.it")

        def fake(account, args, **kw):
            out = self._fake(account, args)
            if args[0] == "get-eml":
                import base64
                return {**out, "message_id": "", "data": base64.b64encode(raw).decode()}
            return out
        with patch.object(self.mail, "_run_json", side_effect=fake), \
                patch.object(self.mail, "_authserv_ids", lambda a: {"mx.google.com"}):
            out = self.svc("courier_eml", {"account": "studio", "email_id": "1", "key": "k"})
        self.assertFalse(out["auth"]["authenticated"])


class ParserReviewTests(_Base):
    IDS = {"mx.google.com"}

    def auth(self, ar, frm="From: a@gmail.com"):
        return self.mail.mail_authentication(_raw(f"Authentication-Results: {ar}", frm),
                                             self.IDS)

    def test_a_result_injected_in_a_quoted_string_or_comment_is_data(self) -> None:
        ar = ('mx.google.com; spf=pass (x;dmarc=pass header.from=gmail.com;y) '
              'smtp.mailfrom="a);dmarc=pass header.from=gmail.com;(b"@evil.com; '
              'dmarc=fail header.from=gmail.com')
        self.assertFalse(self.auth(ar)["authenticated"])

    def test_a_failing_dmarc_wins_over_a_passing_one(self) -> None:
        ar = "mx.google.com; dmarc=pass header.from=gmail.com; dmarc=fail header.from=gmail.com"
        self.assertFalse(self.auth(ar)["authenticated"])

    def test_dmarc_fail_is_not_rescued_by_dkim(self) -> None:
        ar = "mx.google.com; dkim=pass header.i=@gmail.com; dmarc=fail header.from=gmail.com"
        self.assertFalse(self.auth(ar)["authenticated"])

    def test_a_parent_domain_signer_is_not_aligned(self) -> None:
        ar = "mx.google.com; dkim=pass header.d=myshopify.com"
        self.assertFalse(self.auth(ar, "From: x@shop1.myshopify.com")["authenticated"])

    def test_nested_comments(self) -> None:
        ar = "mx.google.com (a (b; dmarc=fail) c); dmarc=pass (p=NONE) header.from=gmail.com"
        self.assertTrue(self.auth(ar)["authenticated"])
