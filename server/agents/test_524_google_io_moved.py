"""google-pack's gateway component's `google_io` (moved from clodia-tools
`server/tools/test_524_google_io.py` when the gateway's native Google client
was removed, clodia-platform#524). The vault is reached through the host.

The Google seam (clodia-platform#524, step 2): `google_io`.

Three things are pinned here, none of which touches the network:

1. **Wire parity with the discovery client.** For every Google method the
   gateway uses, the HTTP request `google_io` sends (method, URL, query string,
   JSON body, multipart framing, media bytes) is compared with the request the
   discovery client builds for the same arguments — from the static discovery
   documents shipped with `googleapiclient`, without executing it. The same
   check validates `_google_fake.OPERATIONS`, the REST↔discovery table the
   Google suites rely on. Skipped once `googleapiclient` is removed (#524
   step 7): by then this file has done its job.
2. **The transport**: decoding, empty answers, errors, one retry on 401,
   downloads returned byte for byte.
3. **The token**: refresh form, cache until expiry minus the margin, errors
   that name the cause and never a secret, nothing secret in the logs.
"""
from __future__ import annotations

import email.parser
import email.policy
import io
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from .google_fake_524 import OPERATIONS, kwargs, operation
from .google_moved_524 import MODS, google_io

try:  # pragma: no cover — depends on the environment
    import httplib2
    from googleapiclient.discovery import build as _build
    from googleapiclient.http import MediaFileUpload
except Exception:  # noqa: BLE001
    _build = None

TOKEN = "ya29.test-access-token"


class _Resp:
    def __init__(self, content: bytes = b"{}"):
        self._c = content

    def read(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Wire:
    """Captures the `urllib` requests and answers them in order."""

    def __init__(self, *answers):
        self.answers = list(answers) or [_Resp()]
        self.sent: list = []

    def __call__(self, req, timeout=None):
        self.sent.append(req)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def _http_error(code: int, body: dict | bytes) -> urllib.error.HTTPError:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(raw))


def _split(url: str) -> tuple[str, dict]:
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.netloc}{u.path}", dict(urllib.parse.parse_qsl(u.query))


def _parts(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body)
    return [(p.get_content_type(), p.get_payload(decode=True)) for p in msg.iter_parts()]


class _TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        google_io.forget()
        self.addCleanup(google_io.forget)

    def file(self, name: str, data: bytes) -> str:
        p = os.path.join(self.dir, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p


@unittest.skipIf(_build is None, "googleapiclient not installed")
class WireParityTests(_TempDir):
    """`google_io` sends what the discovery client sent."""

    SERVICES: dict = {}

    @classmethod
    def svc(cls, api, version):
        if (api, version) not in cls.SERVICES:
            cls.SERVICES[(api, version)] = _build(
                api, version, http=httplib2.Http(), static_discovery=True)
        return cls.SERVICES[(api, version)]

    def reference(self, req: dict):
        """The discovery client's HttpRequest for the same call, not executed."""
        target = self.svc(req["api"], req["version"])
        *resources, method = operation(req).split(".")
        for r in resources:
            target = getattr(target, r)()
        kw = kwargs(req)
        if "media_body" in kw:
            m = kw["media_body"]
            kw["media_body"] = MediaFileUpload(m._filename, mimetype=m.mimetype(),
                                               resumable=False)
        return getattr(target, method)(**kw)

    def sent(self, method_path, api="drive", version="v3", answer=b"{}", **kw):
        wire = _Wire(_Resp(answer))
        with patch.object(google_io, "access_token", lambda _a: TOKEN), \
                patch.object(google_io, "_open", wire):
            out = google_io.call("conto", api, version, method_path, **kw)
        self.assertEqual(len(wire.sent), 1)
        req = google_io.request(api, version, method_path, **kw)
        return wire.sent[0], self.reference(req), out

    def assert_same(self, ours, ref):
        self.assertEqual(ours.get_method(), ref.method)
        self.assertEqual(_split(ours.full_url), _split(ref.uri))
        self.assertEqual(ours.get_header("Authorization"), f"Bearer {TOKEN}")
        ct_ours = ours.get_header("Content-type") or ""
        ct_ref = (ref.headers or {}).get("content-type", "")
        if ref.body is None:
            self.assertIsNone(ours.data)
        elif ct_ref.startswith("multipart/related"):
            self.assertTrue(ct_ours.startswith("multipart/related"))
            a, b = _parts(ct_ours, ours.data), _parts(ct_ref, ref.body)
            self.assertEqual([t for t, _ in a], [t for t, _ in b])
            self.assertEqual(json.loads(a[0][1]), json.loads(b[0][1]))
            self.assertEqual(a[1][1], b[1][1])
        elif ct_ref == "application/json":
            self.assertEqual(ct_ours, "application/json")
            self.assertEqual(json.loads(ours.data), json.loads(ref.body))
        else:                                   # simple media upload
            self.assertEqual(ct_ours, ct_ref)
            self.assertEqual(ours.data, ref.body)

    CASES = [
        ("GET files/{fileId}", "drive", "v3",
         {"params": {"fileId": "a b/c", "fields": "id, parents, shortcutDetails",
                     "supportsAllDrives": True}}),
        ("GET files", "drive", "v3",
         {"params": {"q": "trashed = false and 'X' in parents", "pageSize": 50,
                     "fields": "files(id, name)", "orderBy": "folder,name",
                     "corpora": "allDrives", "supportsAllDrives": True,
                     "includeItemsFromAllDrives": True}}),
        ("POST files", "drive", "v3",
         {"body": {"name": "New", "mimeType": "application/vnd.google-apps.folder",
                   "parents": ["P"]},
          "params": {"fields": "id, name", "supportsAllDrives": True}}),
        ("PATCH files/{fileId}", "drive", "v3",
         {"params": {"fileId": "F", "addParents": "D", "removeParents": None,
                     "fields": "id", "supportsAllDrives": True}}),
        ("PATCH files/{fileId}", "drive", "v3",
         {"body": {"name": "Nuovo"},
          "params": {"fileId": "F", "fields": "id", "supportsAllDrives": True}}),
        ("POST files/{fileId}/permissions", "drive", "v3",
         {"body": {"type": "user", "role": "writer", "emailAddress": "a@b.it"},
          "params": {"fileId": "F", "sendNotificationEmail": True,
                     "supportsAllDrives": True, "fields": "id"}}),
        ("POST documents", "docs", "v1", {"body": {"title": "T"}}),
        ("GET documents/{documentId}", "docs", "v1", {"params": {"documentId": "D"}}),
        ("POST documents/{documentId}:batchUpdate", "docs", "v1",
         {"params": {"documentId": "D"}, "body": {"requests": [
             {"insertText": {"location": {"index": 1}, "text": "ciao"}}]}}),
        ("GET spreadsheets/{spreadsheetId}", "sheets", "v4",
         {"params": {"spreadsheetId": "S",
                     "fields": "sheets.properties(title,sheetId,index,gridProperties)"}}),
        ("POST spreadsheets/{spreadsheetId}:batchUpdate", "sheets", "v4",
         {"params": {"spreadsheetId": "S"},
          "body": {"requests": [{"addSheet": {"properties": {"title": "T"}}}]}}),
        ("GET spreadsheets/{spreadsheetId}/values/{range}", "sheets", "v4",
         {"params": {"spreadsheetId": "S", "range": "Foglio 1!A1:B2",
                     "valueRenderOption": "FORMULA"}}),
        ("POST spreadsheets/{spreadsheetId}/values/{range}:append", "sheets", "v4",
         {"params": {"spreadsheetId": "S", "range": "Spese 2026",
                     "valueInputOption": "USER_ENTERED",
                     "insertDataOption": "INSERT_ROWS"},
          "body": {"values": [["2026-10-16", "=SUM(B1:B3)", 12.5]]}}),
        ("PUT spreadsheets/{spreadsheetId}/values/{range}", "sheets", "v4",
         {"params": {"spreadsheetId": "S", "range": "T!A1:B1",
                     "valueInputOption": "USER_ENTERED"},
          "body": {"values": [["x", "y"]]}}),
        ("GET users/me/calendarList", "calendar", "v3", {}),
        ("GET calendars/{calendarId}/events", "calendar", "v3",
         {"params": {"calendarId": "agenda@tomato.blue", "singleEvents": True,
                     "orderBy": "startTime", "maxResults": 250,
                     "timeMin": "2026-10-20T00:00:00+02:00", "q": "call"}}),
        ("POST calendars/{calendarId}/events", "calendar", "v3",
         {"params": {"calendarId": "primary"},
          "body": {"summary": "S", "start": {"date": "2026-10-20"},
                   "end": {"date": "2026-10-21"}}}),
        ("GET calendars/{calendarId}/events/{eventId}", "calendar", "v3",
         {"params": {"calendarId": "primary", "eventId": "E1"}}),
        ("PUT calendars/{calendarId}/events/{eventId}", "calendar", "v3",
         {"params": {"calendarId": "primary", "eventId": "E1"},
          "body": {"id": "E1", "summary": "S"}}),
        ("DELETE calendars/{calendarId}/events/{eventId}", "calendar", "v3",
         {"params": {"calendarId": "primary", "eventId": "E1"}}),
        ("POST freeBusy", "calendar", "v3",
         {"body": {"timeMin": "A", "timeMax": "B", "items": [{"id": "primary"}]}}),
    ]

    def test_json_methods(self):
        for method_path, api, version, kw in self.CASES:
            with self.subTest(method_path=method_path, kw=kw):
                ours, ref, _out = self.sent(method_path, api, version, **kw)
                self.assert_same(ours, ref)

    def test_every_operation_is_exercised(self):
        """The table the suites rely on is the table checked here."""
        seen = {(api, mp.split(" ", 1)[0], mp.split(" ", 1)[1])
                for mp, api, _v, _kw in self.CASES}
        seen.add(("drive", "GET", "files/{fileId}/export"))   # see the download test
        self.assertEqual(seen, set(OPERATIONS))

    def test_multipart_upload(self):
        """`gdrive.upload`: metadata + bytes, mimetype guessed from the name."""
        payload = b"\x00\x01binary\r\n--not-a-boundary\r\n\xff" * 50
        src = self.file("report.pdf", payload)
        ours, ref, _ = self.sent(
            "POST files", body={"name": "report.pdf", "parents": ["P"]},
            media=google_io.Media(src),
            params={"fields": "id", "supportsAllDrives": True})
        self.assert_same(ours, ref)
        self.assertEqual(_split(ours.full_url)[1]["uploadType"], "multipart")
        self.assertTrue(ours.full_url.startswith(
            "https://www.googleapis.com/upload/drive/v3/files?"))
        self.assertEqual(_parts(ours.get_header("Content-type"), ours.data)[1],
                         ("application/pdf", payload))

    def test_media_upload(self):
        """`gdrive.update`: bytes only, with the mimetype the verb chose."""
        payload = "# Offerta\n\nè così\n".encode()
        src = self.file("offerta.md", payload)
        ours, ref, _ = self.sent(
            "PATCH files/{fileId}", media=google_io.Media(src, "text/markdown"),
            params={"fileId": "DOC1", "fields": "id", "supportsAllDrives": True})
        self.assert_same(ours, ref)
        self.assertEqual(ours.data, payload)
        self.assertEqual(_split(ours.full_url)[1]["uploadType"], "media")

    def test_unguessable_mimetype_is_octet_stream(self):
        src = self.file("blob.zzz-unknown", b"x")
        self.assertEqual(google_io.Media(src).content_type(), "application/octet-stream")
        ref = MediaFileUpload(src, resumable=False)
        self.assertEqual(google_io.Media(src).content_type(), ref.mimetype())

    def test_downloads(self):
        """`get_media` and `export_media`: one GET with `alt=media`, raw bytes back."""
        blob = bytes(range(256)) * 4
        ours, _ref, out = self.sent(
            "GET files/{fileId}", answer=blob, download=True,
            params={"fileId": "F", "supportsAllDrives": True})
        ref = self.svc("drive", "v3").files().get_media(fileId="F", supportsAllDrives=True)
        self.assertEqual(_split(ours.full_url), _split(ref.uri))
        self.assertEqual(out, blob)
        ours, _ref, out = self.sent(
            "GET files/{fileId}/export", answer=b"%PDF-1.7", download=True,
            params={"fileId": "F", "mimeType": "application/pdf"})
        ref = self.svc("drive", "v3").files().export_media(
            fileId="F", mimeType="application/pdf")
        self.assertEqual(_split(ours.full_url), _split(ref.uri))
        self.assertEqual(out, b"%PDF-1.7")


class RequestTests(unittest.TestCase):
    def test_shape(self):
        req = google_io.request("sheets", "v4", "GET spreadsheets/{spreadsheetId}/values/{range}",
                                params={"spreadsheetId": "S", "range": "A b!1",
                                        "valueRenderOption": None})
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "spreadsheets/S/values/A%20b%211")
        self.assertEqual(req["path_params"], {"spreadsheetId": "S", "range": "A b!1"})
        self.assertEqual(req["params"], {})
        self.assertEqual(req["template"], "spreadsheets/{spreadsheetId}/values/{range}")

    def test_refusals(self):
        with self.assertRaises(ValueError):
            google_io.request("gmail", "v1", "GET users/me/profile")
        with self.assertRaises(ValueError):
            google_io.request("drive", "v3", "FETCH files")
        with self.assertRaises(ValueError):
            google_io.request("drive", "v3", "GET files/{fileId}")
        with self.assertRaises(ValueError):
            google_io.request("docs", "v1", "POST documents",
                              media=google_io.Media("/dev/null"))


class TransportTests(_TempDir):
    def run_call(self, wire, *a, **kw):
        with patch.object(google_io, "access_token", lambda _a: TOKEN), \
                patch.object(google_io, "_open", wire):
            return google_io.call("conto", "drive", "v3", *a, **kw)

    def test_an_empty_answer_is_an_empty_dict(self):
        out = self.run_call(_Wire(_Resp(b"")), "PATCH files/{fileId}",
                            params={"fileId": "F"})
        self.assertEqual(out, {})

    def test_an_http_error_carries_status_and_reason_not_the_token(self):
        wire = _Wire(_http_error(404, {"error": {"code": 404,
                                                 "message": "File not found: F."}}))
        with self.assertRaises(google_io.HttpError) as ctx:
            self.run_call(wire, "GET files/{fileId}",
                          params={"fileId": "F", "supportsAllDrives": True})
        e = ctx.exception
        self.assertEqual(e.status, 404)
        self.assertEqual(type(e).__name__, "HttpError")
        self.assertIn("File not found: F.", str(e))
        self.assertNotIn(TOKEN, str(e))
        self.assertNotIn("supportsAllDrives", str(e))      # no query string either

    def test_a_401_retries_once_with_a_fresh_token(self):
        tokens = iter(["old", "new"])
        wire = _Wire(_http_error(401, {"error": {"message": "Invalid Credentials"}}),
                     _Resp(b'{"id": "F"}'))
        forgotten = []
        with patch.object(google_io, "access_token", lambda _a: next(tokens)), \
                patch.object(google_io, "forget", lambda a=None: forgotten.append(a)), \
                patch.object(google_io, "_open", wire):
            out = google_io.call("conto", "drive", "v3", "GET files/{fileId}",
                                 params={"fileId": "F"})
        self.assertEqual(out, {"id": "F"})
        self.assertEqual([r.get_header("Authorization") for r in wire.sent],
                         ["Bearer old", "Bearer new"])
        self.assertEqual(forgotten, ["conto"])

    def test_a_second_401_is_raised(self):
        err = _http_error(401, {"error": {"message": "Invalid Credentials"}})
        wire = _Wire(err, err)
        with self.assertRaises(google_io.HttpError):
            self.run_call(wire, "GET files", params={})
        self.assertEqual(len(wire.sent), 2)

    def test_other_errors_are_not_retried(self):
        wire = _Wire(_http_error(500, b"<html>backend</html>"))
        with self.assertRaises(google_io.HttpError) as ctx:
            self.run_call(wire, "GET files", params={})
        self.assertEqual(len(wire.sent), 1)
        self.assertEqual(ctx.exception.status, 500)

    def test_the_timeout_is_bounded(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(timeout)
            return _Resp()
        self.run_call(urlopen, "GET files", params={})
        self.assertEqual(seen, [google_io.TIMEOUT_S])


BUNDLE = {"client_id": "CID", "client_secret": "CSECRET-value",
          "refresh_token": "1//refresh-secret", "email": "a@b.it", "account": "conto",
          "scope": "https://mail.google.com/ https://www.googleapis.com/auth/drive"}


class TokenTests(_TempDir):
    def setUp(self):
        super().setUp()
        self.reads = []
        for p in (patch.object(google_io, "_cred", lambda a: f"google_{a}"),
                  patch.object(MODS["_host"], "read_internal",
                               lambda n: self.reads.append(n) or dict(BUNDLE))):
            p.start()
            self.addCleanup(p.stop)

    def test_refresh_form_and_cache(self):
        wire = _Wire(_Resp(json.dumps({"access_token": "AT1", "expires_in": 3599,
                                       "token_type": "Bearer"}).encode()))
        with patch.object(google_io, "_open", wire):
            self.assertEqual(google_io.access_token("conto"), "AT1")
            self.assertEqual(google_io.access_token("conto"), "AT1")
        self.assertEqual(len(wire.sent), 1, "the second call is served from the cache")
        self.assertEqual(self.reads, ["google_conto"])
        req = wire.sent[0]
        self.assertEqual(req.full_url, "https://oauth2.googleapis.com/token")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(dict(urllib.parse.parse_qsl(req.data.decode())), {
            "client_id": "CID", "client_secret": "CSECRET-value",
            "refresh_token": "1//refresh-secret", "grant_type": "refresh_token",
            "scope": BUNDLE["scope"]})

    def test_a_token_close_to_expiry_is_refreshed(self):
        wire = _Wire(_Resp(b'{"access_token": "AT1", "expires_in": 3600}'),
                     _Resp(b'{"access_token": "AT2", "expires_in": 3600}'))
        now = [1000.0]
        with patch.object(google_io, "_open", wire), \
                patch.object(google_io.time, "monotonic", lambda: now[0]):
            self.assertEqual(google_io.access_token("conto"), "AT1")
            now[0] += 3600 - google_io.TOKEN_MARGIN_S - 1
            self.assertEqual(google_io.access_token("conto"), "AT1")
            now[0] += 2
            self.assertEqual(google_io.access_token("conto"), "AT2")

    def test_forget_drops_the_cached_token(self):
        wire = _Wire(_Resp(b'{"access_token": "AT1", "expires_in": 3600}'),
                     _Resp(b'{"access_token": "AT2", "expires_in": 3600}'))
        with patch.object(google_io, "_open", wire):
            google_io.access_token("conto")
            google_io.forget("conto")
            self.assertEqual(google_io.access_token("conto"), "AT2")

    def test_a_refused_refresh_names_the_cause_and_no_secret(self):
        wire = _Wire(_http_error(400, {"error": "invalid_grant",
                                       "error_description": "Token has been expired or revoked."}))
        with patch.object(google_io, "_open", wire), \
                self.assertRaises(google_io.RefreshError) as ctx:
            google_io.access_token("conto")
        msg = str(ctx.exception)
        self.assertEqual(msg, "invalid_grant: Token has been expired or revoked.")
        for secret in ("CSECRET-value", "1//refresh-secret"):
            self.assertNotIn(secret, msg)
        self.assertNotIn("conto", google_io._tokens)

    def test_nothing_secret_reaches_the_logs(self):
        records = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        h = _H(level=logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, h)
        self.addCleanup(root.setLevel, old)
        wire = _Wire(_Resp(b'{"access_token": "AT-secret", "expires_in": 3600}'),
                     _Resp(b'{"id": "F"}'))
        with patch.object(google_io, "_open", wire):
            google_io.call("conto", "drive", "v3", "GET files/{fileId}",
                           params={"fileId": "F"})
        for line in records:
            for secret in ("AT-secret", "CSECRET-value", "1//refresh-secret"):
                self.assertNotIn(secret, line)

    def test_the_cache_deadline_uses_a_monotonic_clock(self):
        """Wall-clock jumps (NTP, suspend) must not extend a token's life."""
        wire = _Wire(_Resp(b'{"access_token": "AT1", "expires_in": 3600}'))
        with patch.object(google_io, "_open", wire), \
                patch.object(google_io.time, "time", lambda: 10 ** 12):
            google_io.access_token("conto")
        _tok, deadline = google_io._tokens["google_conto"]
        self.assertLess(deadline, time.monotonic() + 3600)




class TokenStaysOnItsHostTests(unittest.TestCase):
    """Review of #380: urllib forwarded `Authorization` on a redirect to any
    host. A cross-host (or non-https) redirect must arrive without it."""

    def _redir(self, old, new):
        import io
        from .google_moved_524 import google_io as G
        req = urllib.request.Request(old, headers={"Authorization": "Bearer ya29.x"})
        h = G._NoTokenAcrossHosts()
        fp = io.BytesIO(b"")
        return h.redirect_request(req, fp, 302, "Found", {}, new)

    def test_cross_host_drops_the_token(self):
        new = self._redir("https://www.googleapis.com/drive/v3/files",
                          "https://evil.example/x")
        self.assertNotIn("Authorization", {k.title() for k in new.headers})

    def test_downgrade_to_http_drops_the_token(self):
        new = self._redir("https://www.googleapis.com/a", "http://www.googleapis.com/a")
        self.assertNotIn("Authorization", {k.title() for k in new.headers})

    def test_same_host_keeps_it(self):
        new = self._redir("https://www.googleapis.com/a", "https://www.googleapis.com/b")
        self.assertEqual(new.headers.get("Authorization"), "Bearer ya29.x")


class TokenCacheFollowsTheVaultTests(unittest.TestCase):
    def test_redepositing_a_google_credential_drops_the_cached_tokens(self):
        """The gateway's vault calls the component's `credential_changed`
        service when a Google credential is deposited or removed."""
        from .google_moved_524 import HOST
        from .google_moved_524 import google_io as G
        G._tokens["google_x"] = ("ya29.old", 10**12)
        HOST.services["credential_changed"]("google_x")
        self.assertNotIn("google_x", G._tokens)
        G._tokens.clear()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
