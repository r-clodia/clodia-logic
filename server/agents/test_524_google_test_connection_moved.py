"""The Google card's connection test (clodia-platform#284/#285), on google-pack's
gateway component (`routes.test_connection`, service `test_connection`).

Moved from clodia-tools `server/test_284_google_test_connection.py` and
`server/test_285_google_consent_scope.py` when the gateway's native Google
client was removed (#524). The unified `google_<account>` card only: the
legacy `gworkspace_`/`gmail_` cards were the native client's and have no
component counterpart. The HTTP seam is the component's `google_io._open`
(token endpoint, then `userinfo`), answered here by `_Google`.
"""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from .google_moved_524 import HOST, google_io, google_oauth, rebind, routes

UNIFIED = google_oauth.UNIFIED_SCOPE
BUNDLE = {
    "client_id": "1234.apps.googleusercontent.com",
    "client_secret": "il-segreto-dell-app",  # gitleaks:allow — fake test credential
    "refresh_token": "il-refresh-token",  # gitleaks:allow — fake test credential
    "email": "owner@example.com",
    "scope": UNIFIED,
}
TOKEN_OK = {"access_token": "at-1", "scope": UNIFIED}
USERINFO_OK = {"email": "owner@example.com"}


class _R(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Google:
    """`google_io._open`: `token(form)` and `userinfo()` give `(status, payload)`
    or raise; a non-200 becomes the HTTPError urllib would raise."""

    def __init__(self, token=lambda form: (200, TOKEN_OK),
                 userinfo=lambda: (200, USERINFO_OK)):
        self.token, self.userinfo, self.calls = token, userinfo, []

    def __call__(self, req, timeout=None):
        if req.full_url == google_oauth.TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            self.calls.append("token")
            status, payload = self.token(form)
        else:
            self.calls.append("userinfo")
            status, payload = self.userinfo()
        raw = json.dumps(payload).encode()
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return _R(raw)


def _minus(scope: str, piece: str) -> str:
    return " ".join(s for s in scope.split() if piece not in s)


class Base(unittest.TestCase):
    def _probe(self, creds: dict, google: _Google | None = None) -> dict:
        rebind()
        old = dict(HOST.vault)
        HOST.vault.clear()
        HOST.vault.update(creds)
        try:
            with patch.object(google_io, "_open", google or _Google()):
                return routes.test_connection()
        finally:
            HOST.vault.clear()
            HOST.vault.update(old)


class GoogleTestConnectionTests(Base):
    def test_the_service_is_the_cards_test(self):
        rebind()
        self.assertIs(HOST.services["test_connection"], routes.test_connection)

    def test_a_working_credential_names_the_account_it_reached(self):
        r = self._probe({"google_owner": BUNDLE},
                        _Google(userinfo=lambda: (200, {"email": "vero@example.com"})))
        self.assertTrue(r["ok"])
        self.assertIn("vero@example.com", r["detail"])

    def test_a_revoked_consent_is_red_and_says_what_to_do(self):
        r = self._probe({"google_owner": BUNDLE},
                        _Google(token=lambda f: (400, {"error": "invalid_grant"})))
        self.assertIs(r["ok"], False)
        self.assertIn("riconnett", r["detail"].lower())

    def test_an_incomplete_consent_is_not_a_broken_connection(self):
        r = self._probe({"google_owner": BUNDLE}, _Google(
            token=lambda f: (200, {"access_token": "at-1", "scope": _minus(UNIFIED, "drive")})))
        self.assertTrue(r["ok"])
        self.assertIn("Drive", r["detail"])

    def test_no_google_account_is_not_a_failure(self):
        r = self._probe({})
        self.assertIs(r["ok"], None)
        self.assertIn("nessun", r["detail"].lower())

    def test_a_credential_missing_fields_never_reaches_the_network(self):
        g = _Google()
        r = self._probe({"google_owner": {k: v for k, v in BUNDLE.items()
                                          if k != "refresh_token"}}, g)
        self.assertIs(r["ok"], False)
        self.assertIn("refresh_token", r["detail"])
        self.assertEqual(g.calls, [])

    def test_the_secret_never_appears_in_the_result(self):
        for g in (_Google(), _Google(token=lambda f: (400, {"error": "invalid_grant"}))):
            r = self._probe({"google_owner": BUNDLE}, g)
            self.assertNotIn(BUNDLE["client_secret"], r["detail"])
            self.assertNotIn(BUNDLE["refresh_token"], r["detail"])

    def test_one_broken_account_among_many_is_reported_by_name(self):
        def token(form):
            if form.get("refresh_token") == "rotto":
                return 400, {"error": "invalid_grant"}
            return 200, TOKEN_OK
        r = self._probe({"google_buono": BUNDLE,
                         "google_rotto": {**BUNDLE, "refresh_token": "rotto"}},
                        _Google(token=token))
        self.assertIs(r["ok"], False)
        self.assertIn("rotto", r["detail"])
        self.assertIn("buono", r["detail"])

    def test_a_network_failure_is_not_a_bad_token(self):
        def token(form):
            raise urllib.error.URLError("connection reset")
        r = self._probe({"google_owner": BUNDLE}, _Google(token=token))
        self.assertIs(r["ok"], False)
        self.assertIn("rete", r["detail"].lower())

    def test_the_unified_card_does_not_test_the_legacy_credentials(self):
        r = self._probe({"gmail_vecchio": BUNDLE, "gworkspace_vecchio": BUNDLE})
        self.assertIs(r["ok"], None)

    def test_a_response_without_scope_is_not_a_consent_without_scope(self):
        r = self._probe({"google_owner": BUNDLE},
                        _Google(token=lambda f: (200, {"access_token": "at-1"})))
        self.assertTrue(r["ok"], r["detail"])
        self.assertNotIn("fuori dal consenso", r["detail"])
        for service in ("Gmail", "Drive", "Docs", "Calendar"):
            self.assertNotIn(service, r["detail"])

    def test_a_scope_field_that_is_there_and_short_is_still_reported(self):
        r = self._probe({"google_owner": BUNDLE}, _Google(
            token=lambda f: (200, {"access_token": "at-1",
                                   "scope": _minus(UNIFIED, "documents")})))
        self.assertTrue(r["ok"], r["detail"])
        self.assertIn("Docs", r["detail"])

    def test_the_scaffolding_scopes_are_not_named_as_missing_services(self):
        scope = _minus(_minus(UNIFIED, "userinfo.email"), "openid")
        r = self._probe({"google_owner": BUNDLE}, _Google(
            token=lambda f: (200, {"access_token": "at-1", "scope": scope})))
        self.assertTrue(r["ok"], r["detail"])
        self.assertNotIn("fuori dal consenso", r["detail"])


if __name__ == "__main__":
    unittest.main()
