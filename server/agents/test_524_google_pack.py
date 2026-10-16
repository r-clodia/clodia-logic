"""clodia-platform#524 — google-pack declares the Google connectors and ships
their transports.

The gateway keeps the 24 `gdrive/gsheets/gdocs/gcalendar` verbs, the folder
and calendar perimeter and the Google credentials; the pack (a) declares the
four connectors of the gateway's builtin `google.yaml` plus `mail-gmail`, and
(b) ships two gateway-internal transports: `google-transport.request` (one
Google API call, with a short-lived access token) and `gmail-transport.run`
(the mail transport's contract, for Gmail accounts).

What could break silently, and is measured here:

1. **the declaration drifts from the builtin.** On migration day the gateway's
   `google.yaml` leaves service and this one takes its place: any difference
   other than the declared ones (canonicalisation in the pack regex subset, the
   provider's transport, `mail-gmail`) is the gate changing what the owner
   consented to. A copy of the builtin is vendored under `fixtures/`;
2. **the request drifts from the gateway's.** `google.request` must send the
   very request the in-process client sends: URL, query, headers, body,
   multipart framing. Offline the framing is pinned against a table; with
   `CLODIA_TOOLS_SRC` the gateway's own `google_io` builds the same calls and
   the two `urllib` requests are compared byte for byte, and its `_pack_send`
   decodes this transport's answers into exactly what `_send` returns;
3. **the token leaks.** Never in an error, never across a redirect off its
   host;
4. **the Gmail transport drifts from comms-pack's.** Same validation, same
   files, same `{dir}` rule — and the client is the gateway's copy.

With `CLODIA_TOOLS_SRC=<clodia-tools checkout>` the manifest also goes through
the gateway's own parser and registry (as clodia-packs' it-pack tests do).
Without it those tests are skipped and the rest runs offline.
"""
from __future__ import annotations

import ast
import base64
import contextlib
import copy
import http.server
import importlib.util
import inspect
import io
import json
import os
import re
import sys
import tempfile
import textwrap
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import yaml

from ..config import workspace_path

PACK = Path(workspace_path("catalogs/packs/google-pack"))
PLUGIN = PACK / "plugins" / "google-pack"
MCP_DIR = PLUGIN / "mcp"
GOOGLE_CORE = MCP_DIR / "google_transport_core.py"
GOOGLE_ENTRY = MCP_DIR / "google_transport_mcp.py"
GMAIL_CORE = MCP_DIR / "gmail_transport_core.py"
GMAIL_ENTRY = MCP_DIR / "gmail_transport_mcp.py"
CLIENT = MCP_DIR / "email_client.py"
MCP_JSON = PLUGIN / ".mcp.json"
COMMS_MCP = Path(workspace_path("catalogs/packs/comms-pack/plugins/comms-pack/mcp"))
BUILTIN = Path(__file__).resolve().parent / "fixtures" / "524_builtin_google.yaml"
TOOLS_SRC = os.environ.get("CLODIA_TOOLS_SRC", "")

TOKEN = "ya29.a0FAKE524-short-lived_access.token-0123456789"  # gitleaks:allow — fake test credential
REFRESH = "1//0gFAKE524-refresh-token-abcdefghijklmnop"  # gitleaks:allow — fake test credential
CLIENT_SECRET = "GOCSPX-FAKE524-client-secret-xyz"  # gitleaks:allow — fake test credential
MINTED = "ya29.A0minted-access-token_xyz"  # gitleaks:allow — fake test credential

GOOGLE_IDS = ["google", "google-sheets", "google-docs", "google-calendar"]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before running: dataclasses (the gateway's manifest.py) look
    # their module up in sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _gcore():
    return _load(GOOGLE_CORE, "google_transport_core_524")


def _mcore():
    return _load(GMAIL_CORE, "gmail_transport_core_524")


def _pack_doc() -> dict:
    return yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))


def _builtin_doc() -> dict:
    return yaml.safe_load(BUILTIN.read_text(encoding="utf-8"))


def _connector(doc: dict, cid: str) -> dict:
    for entry in doc.get("connectors") or []:
        if isinstance(entry, dict) and entry.get("id") == cid:
            return entry
    raise AssertionError(f"no connector '{cid}'")


def _scheme(connector: dict, name: str) -> dict:
    for s in connector.get("schemes") or []:
        if s["scheme"] == name:
            return s
    raise AssertionError(f"no scheme '{name}'")


def _b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode()


def _canon(rules: list[dict], uri: str) -> str | None:
    """SCHEMA.md: first rule whose `from` FULLMATCHES rewrites; `{n}` = group n."""
    for rule in rules:
        m = re.fullmatch(rule["from"], uri)
        if m:
            return re.sub(r"\{(\d+)\}", lambda g: m.group(int(g.group(1))), rule["to"])
    return None


# ══════════════════════════════════════════════════════════════════════════
# 1. the declaration
# ══════════════════════════════════════════════════════════════════════════

class DeclarationMatchesTheBuiltinTests(unittest.TestCase):
    """The four Google connectors are the builtin plus the declared differences."""

    def setUp(self) -> None:
        self.pack = _pack_doc()
        self.builtin = _builtin_doc()

    def test_ids_and_order(self) -> None:
        self.assertEqual([c["id"] for c in self.pack["connectors"]],
                         GOOGLE_IDS + ["mail-gmail"])
        self.assertEqual([c["id"] for c in self.builtin["connectors"]], GOOGLE_IDS)

    def test_same_connectors_except_the_declared_differences(self) -> None:
        for cid in GOOGLE_IDS:
            with self.subTest(connector=cid):
                pack = copy.deepcopy(_connector(self.pack, cid))
                builtin = _connector(self.builtin, cid)
                if cid == "google":
                    # Difference 2: the provider's transport.
                    self.assertEqual(pack["providers"][0].pop("transport"),
                                     {"backend": "google-transport", "tool": "request"})
                    # Difference 1: canonicalisation in the pack subset —
                    # compared by MEANING in CanonicalisationTests.
                    for name in ("gdrive", "gsheets"):
                        _scheme(pack, name)["canonicalise"] = \
                            _scheme(builtin, name)["canonicalise"]
                self.assertEqual(pack, builtin)

    def test_every_field_compared_is_really_there(self) -> None:
        # A comparison of two empty blocks would pass too.
        owner = _connector(self.builtin, "google")
        for key in ("schemes", "providers", "verbs", "topic_actions", "tools_card"):
            self.assertTrue(owner.get(key), key)
        self.assertEqual({s["scheme"] for s in owner["schemes"]},
                         {"gdrive", "gsheets", "gcal"})
        self.assertEqual(len(owner["verbs"]), 16)
        self.assertEqual(len(_connector(self.builtin, "google-sheets")["verbs"]), 5)
        self.assertEqual(_connector(self.builtin, "google-calendar")["taint"],
                         ["gcalendar.list_events"])
        self.assertEqual([a["id"] for a in owner["topic_actions"]], ["drive-folder"])
        ns = {c["id"]: c["namespace"] for c in self.pack["connectors"]}
        self.assertEqual(ns, {"google": "gdrive", "google-sheets": "gsheets",
                              "google-docs": "gdocs", "google-calendar": "gcalendar",
                              "mail-gmail": "gdrive"})

    def test_gmail_is_a_contributor_of_the_mail_vocabulary(self) -> None:
        self.assertEqual(_connector(self.pack, "mail-gmail"), {
            "id": "mail-gmail", "manifest_version": 1, "namespace": "gdrive",
            "label": "Gmail", "extends": "mail",
            "providers": [{"id": "gmail", "label": "Gmail",
                           "transport": {"backend": "gmail-transport", "tool": "run"}}]})

    def test_no_vault_entry_and_no_reserved_prefix(self) -> None:
        # google_/gmail_/gworkspace_/app_ stay the gateway's: the pack names no
        # vault entry at all, and declares no service with credentials.
        text = (PACK / "pack.yaml").read_text(encoding="utf-8")
        self.assertNotIn("vault_entry", text)
        for c in self.pack["connectors"]:
            self.assertNotIn("services", c)
            self.assertNotIn("storage_remote", c)
            for p in c.get("providers") or []:
                cred = p.get("credential")
                if cred is not None:
                    self.assertEqual(cred["kind"], "oauth", p["id"])

    def test_pack_metadata(self) -> None:
        self.assertEqual(self.pack["name"], "google-pack")
        self.assertEqual(self.pack["version"], "1.0.0")
        self.assertTrue(self.pack["first_party"])
        self.assertEqual(self.pack["upstream"], {"repo": "r-clodia/clodia-logic",
                                                 "path": "catalogs/packs/google-pack",
                                                 "ref": "main"})
        self.assertEqual(self.pack["plugins"], ["google-pack"])

    def test_reserved_as_a_first_party_name(self) -> None:
        from ..api import pack_import, plugin_import
        self.assertIn("google-pack", plugin_import.RESERVED_PLUGIN_NAMES)
        self.assertIn("google-pack", pack_import.RESERVED_PACK_NAMES)


#: URIs a person or a call may carry, and what the builtin makes of them.
_URIS = [
    "gdrive://1AbC-d_E", "gdrive://1AbC-d_E/", "gdrive://folder/1AbC-d_E",
    "gdrive://folder/1AbC-d_E/", "gdrive://folder/", "gdrive://a/b",
    "https://drive.google.com/drive/folders/1AbC-d_E",
    "http://drive.google.com/drive/folders/1AbC-d_E",
    "https://drive.google.com/drive/folders/1AbC-d_E/",
    "https://drive.google.com/drive/folders/1AbC-d_E?usp=sharing",
    "https://drive.google.com/drive/folders/1AbC-d_E#x",
    "https://drive.google.com/drive/u/0/folders/1AbC-d_E",
    "https://drive.google.com/drive/u/12/folders/1AbC-d_E?usp=drive_link",
    "https://drive.google.com/drive/folders/1AbC-d_Eextra stuff",
    "https://drive.google.com/drive/folders/",
    "https://drive.google.com/drive/my-drive",
    "https://docs.google.com/document/d/DOC_1-x",
    "https://docs.google.com/document/d/DOC_1-x/edit",
    "https://docs.google.com/document/d/DOC_1-x/edit?tab=t.0#heading=h.1",
    "https://drive.google.com/file/d/FILE-1/view?usp=sharing",
    "https://drive.google.com/file/d/FILE-1",
    "https://docs.google.com/spreadsheets/d/SHEET_1/edit#gid=0",
    "https://docs.google.com/spreadsheets/d/SHEET_1",
    "gsheets://SHEET_1", "gsheets://SHEET_1/", "gsheets://SHEET_1/x",
    "https://drive.google.com.evil.example/drive/folders/X",
    "https://evil.example/drive/folders/X", "gdrive:folder/X", "gcal:a@b.it",
]


class CanonicalisationTests(unittest.TestCase):
    """Difference 1: the rules are rewritten, the answers are not."""

    def setUp(self) -> None:
        self.pack = _connector(_pack_doc(), "google")
        self.builtin = _connector(_builtin_doc(), "google")

    def _rules(self, connector: dict) -> list[dict]:
        out = []
        for s in connector["schemes"]:
            out += s.get("canonicalise") or []
        return out

    def test_same_answers_as_the_builtin(self) -> None:
        pack, builtin = self._rules(self.pack), self._rules(self.builtin)
        rewritten = 0
        for uri in _URIS:
            with self.subTest(uri=uri):
                want = _canon(builtin, uri)
                self.assertEqual(_canon(pack, uri), want)
                rewritten += want is not None
        self.assertGreater(rewritten, 15)       # the table is not vacuous

    def test_the_one_bounded_difference(self) -> None:
        # `\d\d?` instead of `\d+`: account index 100 and above is not a URL a
        # person copies, and the pack grammar has no unbounded group.
        uri = "https://drive.google.com/drive/u/123/folders/X"
        self.assertEqual(_canon(self._rules(self.builtin), uri), "gdrive:folder/X")
        self.assertIsNone(_canon(self._rules(self.pack), uri))

    def test_rules_write_only_into_their_own_scheme(self) -> None:
        for s in self.pack["schemes"]:
            for rule in s.get("canonicalise") or []:
                self.assertTrue(rule["to"].startswith(s["scheme"] + ":"), rule)

    def test_patterns_stay_in_the_pack_subset(self) -> None:
        # The cheap structural echo of the gateway's rule (the real validator
        # runs in GatewayValidatorTests when CLODIA_TOOLS_SRC is set): no
        # quantified group, at most two repeats and two `?`, no alternation.
        for s in self.pack["schemes"]:
            patterns = [r["from"] for r in s.get("canonicalise") or []]
            patterns += [f["pattern"] for f in s.get("forms") or []]
            for p in patterns:
                with self.subTest(pattern=p):
                    self.assertNotRegex(p, r"\)[?*+{]")
                    self.assertNotIn("(?:", p)
                    self.assertNotIn("|", p)
                    bare = re.sub(r"\\.", "x", p)              # escapes
                    bare = re.sub(r"\[[^\]]*\]", "C", bare)   # classes
                    self.assertLessEqual(len(re.findall(r"[*+]", bare)), 2)
                    self.assertLessEqual(bare.count("?"), 2)
                    self.assertLessEqual(len(p), 200)

    def test_examples_match_their_forms(self) -> None:
        for s in self.pack["schemes"]:
            for f in s.get("forms") or []:
                self.assertTrue(re.fullmatch(f["pattern"], f["example"]), f)


# ══════════════════════════════════════════════════════════════════════════
# 2. the mount
# ══════════════════════════════════════════════════════════════════════════

class MountTests(unittest.TestCase):
    """`.mcp.json` mounts the two transports the providers name, with no secrets."""

    def setUp(self) -> None:
        self.mount = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]

    def test_backends_named_as_the_providers_say(self) -> None:
        doc = _pack_doc()
        named = {_connector(doc, "google")["providers"][0]["transport"]["backend"]:
                 GOOGLE_ENTRY,
                 _connector(doc, "mail-gmail")["providers"][0]["transport"]["backend"]:
                 GMAIL_ENTRY}
        self.assertEqual(set(self.mount), set(named))
        for backend, entry in named.items():
            with self.subTest(backend=backend):
                args = [str(a) for a in self.mount[backend].get("args") or []]
                self.assertEqual(args, [f"/datadir/plugins/google-pack/mcp/{entry.name}"])
                self.assertTrue(entry.is_file())

    def test_no_vault_placeholder(self) -> None:
        # Credentials arrive per call: an access token in the arguments, the
        # XOAUTH2 files as files. A mount-time secret would give the process
        # an account for its whole life.
        for backend, entry in self.mount.items():
            self.assertNotIn("VAULT", json.dumps(entry), backend)
            self.assertNotIn("env", entry, backend)

    def test_the_tools_the_gateway_calls(self) -> None:
        g, m = _gcore(), _mcore()
        self.assertEqual([t["name"] for t in g.TOOL_SPECS], ["request"])
        self.assertEqual(set(g.DISPATCH), {"request"})
        schema = g.TOOL_SPECS[0]["inputSchema"]
        self.assertEqual(set(schema["properties"]), set(g.ARGS))
        self.assertEqual(set(g.ARGS), {"access_token", "api", "version", "method",
                                       "path", "params", "body", "media_b64",
                                       "media_mime", "download"})
        self.assertEqual([t["name"] for t in m.TOOL_SPECS], ["run"])
        self.assertEqual(set(m.TOOL_SPECS[0]["inputSchema"]["properties"]),
                         {"argv", "files", "timeout"})
        for spec in g.TOOL_SPECS + m.TOOL_SPECS:
            self.assertIn("GATEWAY-INTERNAL", spec["description"])

    def test_the_envelopes_do_not_redeclare_the_surface(self) -> None:
        for entry in (GOOGLE_ENTRY, GMAIL_ENTRY):
            src = entry.read_text(encoding="utf-8")
            self.assertIn("core.TOOL_SPECS", src)
            self.assertNotIn("inputSchema", src)

    def test_the_cores_are_stdlib_only(self) -> None:
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        for core in (GOOGLE_CORE, GMAIL_CORE):
            tree = ast.parse(core.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for n in names:
                    self.assertIn(n.split(".")[0], stdlib, f"{core.name}: {n}")


# ══════════════════════════════════════════════════════════════════════════
# 3. google.request — framing, offline
# ══════════════════════════════════════════════════════════════════════════

BOUNDARY_HEX = "0" * 32
BOUNDARY = "===============" + BOUNDARY_HEX + "=="


def _payload(**kw) -> dict:
    base = {"access_token": TOKEN, "api": "drive", "version": "v3", "method": "GET",
            "path": "files", "params": {}, "body": None, "media_b64": None,
            "media_mime": None, "download": False}
    base.update(kw)
    return base


class _Resp:
    """What `_open` returns: a context manager with `status` and `read()`."""

    def __init__(self, status: int = 200, body: bytes = b"{}"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


class FramingTests(unittest.TestCase):
    """`build()` frames each call as `google_io._send` does (pinned table)."""

    def setUp(self) -> None:
        self.core = _gcore()
        p = patch("secrets.token_hex", return_value=BOUNDARY_HEX)
        p.start()
        self.addCleanup(p.stop)

    def built(self, **kw) -> urllib.request.Request:
        return self.core.build(self.core._check(_payload(**kw)))

    def test_get_with_special_char_id(self) -> None:
        # The gateway percent-encodes path parameters (`quote(v, safe="")`).
        r = self.built(path="files/a%20b%2F%C3%A7%3F%23%25%26%3D%2B",
                       params={"fields": "id,name", "supportsAllDrives": True})
        self.assertEqual(r.full_url,
                         "https://www.googleapis.com/drive/v3/files/a%20b%2F%C3%A7%3F%23"
                         "%25%26%3D%2B?fields=id%2Cname&supportsAllDrives=true&alt=json")
        self.assertEqual(r.get_method(), "GET")
        self.assertIsNone(r.data)
        self.assertEqual(dict(r.header_items()), {"Authorization": f"Bearer {TOKEN}",
                                                  "Accept": "application/json"})

    def test_list_with_booleans_and_repeated_params(self) -> None:
        r = self.built(params={"q": "'F1' in parents and trashed = false",
                               "pageSize": 100, "includeItemsFromAllDrives": True,
                               "supportsAllDrives": False,
                               "spaces": ["drive", "appDataFolder"],
                               "orderBy": None})
        self.assertEqual(r.full_url,
                         "https://www.googleapis.com/drive/v3/files?q=%27F1%27+in+parents"
                         "+and+trashed+%3D+false&pageSize=100&includeItemsFromAllDrives=true"
                         "&supportsAllDrives=false&spaces=drive&spaces=appDataFolder&alt=json")

    def test_multipart_upload(self) -> None:
        meta = {"name": "Relazione è.md", "parents": ["F1"]}
        media = "# titolo\n".encode() + bytes(range(256))
        r = self.built(method="POST", body=meta, media_b64=_b64(media),
                       media_mime="text/markdown", params={"fields": "id"})
        self.assertEqual(r.full_url, "https://www.googleapis.com/upload/drive/v3/files"
                                     "?fields=id&alt=json&uploadType=multipart")
        self.assertEqual(r.get_header("Content-type"),
                         f'multipart/related; boundary="{BOUNDARY}"')
        want = (f"--{BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps(meta) + f"\r\n--{BOUNDARY}\r\n"
                + "Content-Type: text/markdown\r\n"
                + "Content-Transfer-Encoding: binary\r\n\r\n").encode() \
            + media + f"\r\n--{BOUNDARY}--".encode()
        self.assertEqual(r.data, want)

    def test_media_only_update(self) -> None:
        r = self.built(method="PATCH", path="files/F9", media_b64=_b64(b"%PDF-1.4"),
                       media_mime="application/pdf")
        self.assertEqual(r.full_url, "https://www.googleapis.com/upload/drive/v3/files/F9"
                                     "?alt=json&uploadType=media")
        self.assertEqual(r.data, b"%PDF-1.4")
        self.assertEqual(r.get_header("Content-type"), "application/pdf")
        # No type from the gateway: octet-stream, as `Media.content_type()`.
        r = self.built(method="PATCH", path="files/F9", media_b64=_b64(b"x"))
        self.assertEqual(r.get_header("Content-type"), "application/octet-stream")

    def test_download(self) -> None:
        r = self.built(path="files/F9/export", params={"mimeType": "application/pdf"},
                       download=True)
        self.assertEqual(r.full_url, "https://www.googleapis.com/drive/v3/files/F9/export"
                                     "?mimeType=application%2Fpdf&alt=media")
        self.assertEqual(dict(r.header_items()), {"Authorization": f"Bearer {TOKEN}"})

    def test_delete_without_alt(self) -> None:
        r = self.built(api="calendar", method="DELETE",
                       path="calendars/team%40group.calendar.google.com/events/e1",
                       params={"sendUpdates": "all"})
        self.assertEqual(r.full_url,
                         "https://www.googleapis.com/calendar/v3/calendars/"
                         "team%40group.calendar.google.com/events/e1?sendUpdates=all")
        self.assertEqual(r.get_method(), "DELETE")

    def test_json_body(self) -> None:
        body = {"values": [[1, "à", True, None]]}
        r = self.built(api="sheets", version="v4", method="POST",
                       path="spreadsheets/S1/values/%27Foglio%201%27%21A1%3AC3:append",
                       params={"valueInputOption": "USER_ENTERED"}, body=body)
        self.assertEqual(r.full_url,
                         "https://sheets.googleapis.com/v4/spreadsheets/S1/values/"
                         "%27Foglio%201%27%21A1%3AC3:append?valueInputOption=USER_ENTERED"
                         "&alt=json")
        self.assertEqual(r.data, json.dumps(body).encode())
        self.assertEqual(r.get_header("Content-type"), "application/json")

    def test_an_explicit_alt_is_kept_unless_downloading(self) -> None:
        self.assertTrue(self.built(params={"alt": "proto"}).full_url.endswith("?alt=proto"))
        self.assertTrue(self.built(params={"alt": "proto"}, download=True)
                        .full_url.endswith("?alt=media"))

    def test_docs_root(self) -> None:
        r = self.built(api="docs", version="v1", path="documents/D1")
        self.assertEqual(r.full_url, "https://docs.googleapis.com/v1/documents/D1?alt=json")


class RefusalTests(unittest.TestCase):
    """A request outside the contract is refused — and the token never echoed."""

    def setUp(self) -> None:
        self.core = _gcore()

    def refused(self, **kw) -> str:
        with patch.object(self.core, "_open", side_effect=AssertionError("sent")):
            with self.assertRaises(self.core.ToolFailure) as cm:
                self.core.call("request", _payload(**kw))
        msg = str(cm.exception)
        self.assertNotIn(TOKEN, msg)
        self.assertNotIn("ya29", msg)
        return msg

    def test_paths_that_could_leave_the_api_root(self) -> None:
        for path in ("", "/files", "files/../../oauth2", "files/./x", "a//b",
                     "files?alt=media", "files#x", "files/x y", "https://evil.example/x",
                     "files\\x", "files/\r\nX-Injected: 1", "files/%0d%0a" + "\n",
                     "files/ü"):
            with self.subTest(path=path):
                self.assertIn("'path'", self.refused(path=path))

    def test_bad_fields(self) -> None:
        cases = [dict(api="gmail"), dict(version="v2"), dict(method="TRACE"),
                 dict(access_token=""), dict(access_token=TOKEN + "\r\nX: y"),
                 dict(access_token=None), dict(params=[]), dict(params={"a": {"b": 1}}),
                 dict(params={"": 1}), dict(download="yes"),
                 dict(api="docs", version="v1", media_b64=_b64("x")),
                 dict(media_b64="not base64!!"), dict(media_b64=_b64("x"),
                                                      media_mime="text/plain\r\nX: y"),
                 dict(extra="x")]
        for kw in cases:
            with self.subTest(case=kw):
                self.refused(**kw)

    def test_not_an_object(self) -> None:
        with self.assertRaises(self.core.ToolFailure):
            self.core.call("request", "x")

    def test_unknown_tool(self) -> None:
        with self.assertRaises(self.core.ToolFailure) as cm:
            self.core.call("run", _payload())
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_a_network_error_names_only_its_type(self) -> None:
        err = urllib.error.URLError(f"https://x/?access_token={TOKEN}")
        with patch.object(self.core, "_open", side_effect=err):
            with self.assertRaises(self.core.ToolFailure) as cm:
                self.core.call("request", _payload(params={"q": TOKEN}))
        self.assertEqual(str(cm.exception), "google-transport: request failed (URLError)")
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)

    def test_an_internal_error_names_only_its_type(self) -> None:
        with patch.object(self.core, "build", side_effect=KeyError(TOKEN)):
            with self.assertRaises(self.core.ToolFailure) as cm:
                self.core.call("request", _payload())
        self.assertEqual(str(cm.exception), "google-transport: internal error (KeyError)")


class AnswerTests(unittest.TestCase):
    """The answer contract: `{status, json?, bytes_b64?}`."""

    def setUp(self) -> None:
        self.core = _gcore()

    def answer(self, resp=None, error=None, **kw) -> dict:
        with patch.object(self.core, "_open",
                          side_effect=error) if error else \
                patch.object(self.core, "_open", return_value=resp):
            return json.loads(self.core.call("request", _payload(**kw)))

    def _http_error(self, code: int, body: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://www.googleapis.com/drive/v3/files", code,
                                      "x", {}, io.BytesIO(body))

    def test_json(self) -> None:
        self.assertEqual(self.answer(_Resp(200, b'{"files": [{"id": "a"}]}')),
                         {"status": 200, "json": {"files": [{"id": "a"}]}})

    def test_empty(self) -> None:
        self.assertEqual(self.answer(_Resp(204, b"")), {"status": 204})
        self.assertEqual(self.answer(_Resp(200, b" \n")), {"status": 200})

    def test_download(self) -> None:
        blob = bytes(range(256)) * 3
        out = self.answer(_Resp(200, blob), download=True)
        self.assertEqual(out, {"status": 200, "bytes_b64": _b64(blob)})
        self.assertEqual(self.answer(_Resp(200, b""), download=True),
                         {"status": 200, "bytes_b64": ""})

    def test_errors_are_answers(self) -> None:
        body = b'{"error": {"code": 404, "message": "File not found: X."}}'
        self.assertEqual(self.answer(error=self._http_error(404, body)),
                         {"status": 404, "json": json.loads(body)})
        self.assertEqual(self.answer(error=self._http_error(502, b"<html>bad</html>")),
                         {"status": 502, "bytes_b64": _b64(b"<html>bad</html>")})
        self.assertEqual(self.answer(error=self._http_error(401, b"")), {"status": 401})
        # A download's error body is still an error body, not the file.
        self.assertEqual(self.answer(error=self._http_error(403, body), download=True),
                         {"status": 403, "json": json.loads(body)})

    def test_a_non_json_success_is_a_failure(self) -> None:
        with self.assertRaises(self.core.ToolFailure):
            self.answer(_Resp(200, b"<html>"))

    def test_no_state_between_calls(self) -> None:
        before = {k for k in vars(self.core)}
        self.answer(_Resp(200, b"{}"))
        self.assertEqual({k for k in vars(self.core)}, before)


# ══════════════════════════════════════════════════════════════════════════
# 4. google.request — the network, for real (loopback)
# ══════════════════════════════════════════════════════════════════════════

class _Recorder(http.server.BaseHTTPRequestHandler):
    """Records every request; answers by path."""

    seen: list = []
    redirect_to = ""

    def log_message(self, *a):  # noqa: D401 — quiet
        pass

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        type(self).seen.append({"host": self.headers.get("Host"), "path": self.path,
                                "method": self.command,
                                "auth": self.headers.get("Authorization"), "body": body})
        if "/redirect" in self.path:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "/missing" in self.path:
            out, code = b'{"error": {"message": "nope"}}', 404
        else:
            out, code = json.dumps({"ok": True}).encode(), 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_DELETE = _handle


class LoopbackTests(unittest.TestCase):
    """The real `urllib` path against a local server: the token goes to the API
    host, never to the host a redirect points at."""

    def setUp(self) -> None:
        self.core = _gcore()
        _Recorder.seen = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        port = self.srv.server_address[1]
        roots = {k: f"http://127.0.0.1:{port}/{k[0]}/" for k in self.core.API_ROOTS}
        p = patch.dict(self.core.API_ROOTS, roots)
        p.start()
        self.addCleanup(p.stop)
        self.port = port

    def call(self, **kw) -> dict:
        return json.loads(self.core.call("request", _payload(**kw)))

    def test_a_call_reaches_the_root_with_the_token(self) -> None:
        self.assertEqual(self.call(params={"q": "x"}), {"status": 200, "json": {"ok": True}})
        (seen,) = _Recorder.seen
        self.assertEqual(seen["path"], "/drive/files?q=x&alt=json")
        self.assertEqual(seen["auth"], f"Bearer {TOKEN}")

    def test_an_error_status_is_an_answer(self) -> None:
        self.assertEqual(self.call(path="missing"),
                         {"status": 404, "json": {"error": {"message": "nope"}}})

    def test_a_redirect_off_host_drops_the_token(self) -> None:
        # Same server, another HOST name: what matters is the host the bearer
        # was meant for.
        _Recorder.redirect_to = f"http://localhost:{self.port}/elsewhere"
        self.assertEqual(self.call(path="redirect"), {"status": 200, "json": {"ok": True}})
        first, second = _Recorder.seen
        self.assertEqual(first["auth"], f"Bearer {TOKEN}")
        self.assertEqual(second["path"], "/elsewhere")
        self.assertIsNone(second["auth"])

    def test_the_redirect_rule(self) -> None:
        h = self.core._NoTokenAcrossHosts()

        def follow(src: str, dst: str) -> urllib.request.Request:
            r = urllib.request.Request(src, headers={"Authorization": f"Bearer {TOKEN}"})
            return h.redirect_request(r, None, 302, "Found", {}, dst)

        kept = follow("https://www.googleapis.com/drive/v3/files/x",
                      "https://www.googleapis.com/drive/v3/files/y")
        self.assertEqual(kept.get_header("Authorization"), f"Bearer {TOKEN}")
        for dst in ("https://evil.example/x", "http://www.googleapis.com/x",
                    "https://www.googleapis.com:8443/x",
                    "https://www.googleapis.com.evil.example/x"):
            with self.subTest(dst=dst):
                new = follow("https://www.googleapis.com/drive/v3/files/x", dst)
                self.assertIsNone(new.get_header("Authorization"))
                self.assertNotIn(TOKEN, json.dumps(dict(new.header_items())))

    def test_connection_refused_is_a_generic_failure(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        with self.assertRaises(self.core.ToolFailure) as cm:
            self.call()
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn("127.0.0.1", str(cm.exception))


# ══════════════════════════════════════════════════════════════════════════
# 5. google.request against the gateway's own google_io (CLODIA_TOOLS_SRC)
# ══════════════════════════════════════════════════════════════════════════

def _gateway_google_io():
    """`server/tools/google_io.py` of the checkout, inside a stand-in package so
    that its `from .. import proxy` resolves to a proxy of our own."""
    root = Path(TOOLS_SRC) / "server"
    pkg = types.ModuleType("gw524")
    pkg.__path__ = []
    tools = types.ModuleType("gw524.tools")
    tools.__path__ = []
    proxy = types.ModuleType("gw524.proxy")
    sys.modules.update({"gw524": pkg, "gw524.tools": tools, "gw524.proxy": proxy})
    pkg.tools, pkg.proxy = tools, proxy
    gio = _load(root / "tools" / "google_io.py", "gw524.tools.google_io")
    fake = _load(root / "tools" / "_google_fake.py", "gw524.tools._google_fake")
    return gio, proxy, fake.OPERATIONS


#: (method_path, kwargs for `google_io.request`) — the calls the verbs make,
#: with the awkward values: ids that need escaping, booleans, lists, None.
_CALLS = [
    ("GET files/{fileId}", dict(api="drive", version="v3",
                                params={"fileId": "a b/ç?#%&=+'!", "fields": "id,name,parents",
                                        "supportsAllDrives": True})),
    ("GET files", dict(api="drive", version="v3",
                       params={"q": "'F1' in parents and trashed = false", "pageSize": 100,
                               "includeItemsFromAllDrives": True, "supportsAllDrives": False,
                               "spaces": ["drive", "appDataFolder"], "orderBy": None,
                               "pageToken": None})),
    ("POST files", dict(api="drive", version="v3", params={"fields": "id,parents"},
                        body={"name": "Relazione è.md", "parents": ["F1"]},
                        media=("relazione.md", None, "# Titolo è\n".encode() + bytes(range(256))))),
    ("PATCH files/{fileId}", dict(api="drive", version="v3", params={"fileId": "F9"},
                                  media=("scan.pdf", None, b"%PDF-1.4\n\x00\xff"))),
    ("PATCH files/{fileId}", dict(api="drive", version="v3",
                                  params={"fileId": "F9", "addParents": "P2",
                                          "removeParents": "P1"},
                                  body={"name": "nuovo"})),
    ("GET files/{fileId}", dict(api="drive", version="v3",
                                params={"fileId": "F9", "supportsAllDrives": True},
                                download=True)),
    ("GET files/{fileId}/export", dict(api="drive", version="v3",
                                       params={"fileId": "D1", "mimeType": "application/pdf"},
                                       download=True)),
    ("POST files/{fileId}/permissions", dict(api="drive", version="v3",
                                             params={"fileId": "F9",
                                                     "sendNotificationEmail": False},
                                             body={"role": "reader", "type": "user",
                                                   "emailAddress": "a@example.org"})),
    ("GET documents/{documentId}", dict(api="docs", version="v1",
                                        params={"documentId": "DOC_1-x"})),
    ("POST documents", dict(api="docs", version="v1", body={"title": "Verbale è"})),
    ("GET spreadsheets/{spreadsheetId}",
     dict(api="sheets", version="v4",
          params={"spreadsheetId": "S1", "fields": "sheets.properties",
                  "includeGridData": False})),
    ("POST spreadsheets/{spreadsheetId}:batchUpdate",
     dict(api="sheets", version="v4", params={"spreadsheetId": "S1"},
          body={"requests": [{"addSheet": {"properties": {"title": "Nuova"}}}]})),
    ("POST documents/{documentId}:batchUpdate",
     dict(api="docs", version="v1", params={"documentId": "DOC_1"},
          body={"requests": [{"insertText": {"text": "ciao", "location": {"index": 1}}}]})),
    ("GET spreadsheets/{spreadsheetId}/values/{range}",
     dict(api="sheets", version="v4",
          params={"spreadsheetId": "S1", "range": "'Foglio 1'!A1:C3",
                  "valueRenderOption": "UNFORMATTED_VALUE"})),
    ("POST spreadsheets/{spreadsheetId}/values/{range}:append",
     dict(api="sheets", version="v4",
          params={"spreadsheetId": "S1", "range": "Sheet1!A:Z",
                  "valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS",
                  "includeValuesInResponse": False},
          body={"values": [[1, "à", True, None, 2.5]]})),
    ("PUT spreadsheets/{spreadsheetId}/values/{range}",
     dict(api="sheets", version="v4",
          params={"spreadsheetId": "S1", "range": "A1", "valueInputOption": "RAW"},
          body={"values": [["x"]]})),
    ("GET users/me/calendarList", dict(api="calendar", version="v3", params={})),
    ("GET calendars/{calendarId}/events",
     dict(api="calendar", version="v3",
          params={"calendarId": "team@group.calendar.google.com",
                  "timeMin": "2026-10-16T00:00:00+02:00", "singleEvents": True,
                  "orderBy": "startTime", "maxResults": 250})),
    ("POST calendars/{calendarId}/events",
     dict(api="calendar", version="v3",
          params={"calendarId": "primary", "sendUpdates": "none"},
          body={"summary": "Riunione", "start": {"dateTime": "2026-10-17T10:00:00+02:00"},
                "end": {"dateTime": "2026-10-17T11:00:00+02:00"}})),
    ("GET calendars/{calendarId}/events/{eventId}",
     dict(api="calendar", version="v3", params={"calendarId": "primary", "eventId": "e1"})),
    ("PUT calendars/{calendarId}/events/{eventId}",
     dict(api="calendar", version="v3",
          params={"calendarId": "primary", "eventId": "e1", "sendUpdates": "all"},
          body={"summary": "Spostata"})),
    ("DELETE calendars/{calendarId}/events/{eventId}",
     dict(api="calendar", version="v3",
          params={"calendarId": "primary", "eventId": "e1/2", "sendUpdates": "all"})),
    ("POST freeBusy", dict(api="calendar", version="v3",
                           body={"items": [{"id": "primary"}],
                                 "timeMin": "2026-10-16T00:00:00Z"})),
]


@unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
class GatewayDifferentialTests(unittest.TestCase):
    """The gateway's `google_io` builds the call; its in-process `_send` and
    this pack's `request` must open the SAME urllib request, and the gateway's
    `_pack_send` must decode this pack's answer into what `_send` returns."""

    def setUp(self) -> None:
        self.gio, self.proxy, self.operations = _gateway_google_io()
        self.core = _gcore()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch("secrets.token_hex", return_value=BOUNDARY_HEX)
        p.start()
        self.addCleanup(p.stop)
        # The gateway's pack transport, wired to THIS pack: what crosses MCP is
        # JSON text both ways, and a tool failure is an exception.
        self.wire: list[dict] = []

        def call_backend_tool_sync(backend, tool, arguments, timeout):
            args = json.loads(json.dumps(arguments))
            self.wire.append(args)
            return self.core.call(tool, args)
        self.proxy.call_backend_tool_sync = call_backend_tool_sync

    def req(self, method_path: str, kw: dict) -> dict:
        kw = dict(kw)
        media = kw.pop("media", None)
        if media is not None:
            name, mime, data = media
            path = Path(self.tmp.name) / name
            path.write_bytes(data)
            kw["media"] = self.gio.Media(str(path), mime)
        api, version = kw.pop("api"), kw.pop("version")
        return self.gio.request(api, version, method_path, **kw)

    def opened(self, fn, respond) -> tuple[list, object]:
        """Run `fn` with `_open` of BOTH modules captured; `respond(n)` answers."""
        seen: list = []

        def fake_open(r, timeout):
            seen.append((r.full_url, r.get_method(), sorted(r.header_items()),
                         r.data, timeout))
            return respond(len(seen))
        with patch.object(self.gio, "_open", fake_open), \
                patch.object(self.core, "_open", fake_open):
            try:
                out = fn()
            except Exception as e:  # noqa: BLE001 — compared by the caller
                out = e
        return seen, out

    def test_the_table_covers_every_operation_the_gateway_knows(self) -> None:
        known = {(api, method, tpl) for (api, method, tpl) in self.operations}
        used = set()
        for mp, kw in _CALLS:
            method, _, tpl = mp.partition(" ")
            used.add((kw["api"], method, tpl))
        self.assertEqual(known - used, set())

    def test_byte_identical_requests(self) -> None:
        for mp, kw in _CALLS:
            with self.subTest(call=mp, download=kw.get("download", False)):
                req = self.req(mp, kw)
                ok = lambda n: _Resp(200, b"BYTES" if req["download"] else b'{"id": "x"}')  # noqa: E731
                native, _ = self.opened(lambda: self.gio._send(TOKEN, req), ok)
                packed, _ = self.opened(
                    lambda: self.gio._pack_send(("google-transport", "request"), TOKEN, req),
                    ok)
                self.assertEqual(len(native), 1)
                self.assertEqual(packed, native)
                self.assertEqual(self.wire[-1], self.gio.pack_payload(TOKEN, req))

    def _answers(self) -> list:
        def http_error(code, body):
            # A real server's reason phrase: the standard one. (The pack's
            # contract carries no reason; on a body without an error message
            # the gateway falls back to the standard phrase of the status.)
            import http as _http
            return urllib.error.HTTPError("https://www.googleapis.com/x", code,
                                          _http.HTTPStatus(code).phrase, {},
                                          io.BytesIO(body))
        return [
            ("json", lambda n: _Resp(200, b'{"files": [{"id": "a", "name": "\xc3\xa8"}]}')),
            ("empty", lambda n: _Resp(204, b"")),
            ("blank", lambda n: _Resp(200, b"  ")),
            ("404 json", lambda n: (_ for _ in ()).throw(http_error(
                404, b'{"error": {"code": 404, "message": "File not found: F9."}}'))),
            ("403 string error", lambda n: (_ for _ in ()).throw(http_error(
                403, b'{"error": "rateLimitExceeded"}'))),
            ("500 html", lambda n: (_ for _ in ()).throw(http_error(500, b"<html>oops</html>"))),
            ("401 empty", lambda n: (_ for _ in ()).throw(http_error(401, b""))),
        ]

    def test_answers_decode_to_what_the_in_process_path_returns(self) -> None:
        for mp, kw in _CALLS:
            req = self.req(mp, kw)
            for label, respond in self._answers():
                with self.subTest(call=mp, answer=label):
                    if req["download"] and label in ("json", "empty", "blank"):
                        respond = lambda n: _Resp(200, b"\x00\xffPDF bytes")  # noqa: E731
                    _, native = self.opened(lambda: self.gio._send(TOKEN, req), respond)
                    _, packed = self.opened(
                        lambda: self.gio._pack_send(("google-transport", "request"),
                                                    TOKEN, req), respond)
                    if isinstance(native, self.gio.HttpError):
                        self.assertIsInstance(packed, self.gio.HttpError)
                        self.assertEqual((packed.status, packed.method, packed.url,
                                          packed.reason),
                                         (native.status, native.method, native.url,
                                          native.reason))
                        self.assertNotIn(TOKEN, str(packed))
                    else:
                        self.assertNotIsInstance(native, Exception, native)
                        self.assertEqual(packed, native)

    def test_a_non_json_success_fails_on_both_paths(self) -> None:
        mp, kw = _CALLS[0]
        req = self.req(mp, kw)
        bad = lambda n: _Resp(200, b"<html>")  # noqa: E731
        _, native = self.opened(lambda: self.gio._send(TOKEN, req), bad)
        with self.assertLogs("clodia-tools.google-io", "WARNING") as logs:
            _, packed = self.opened(
                lambda: self.gio._pack_send(("google-transport", "request"), TOKEN, req),
                bad)
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        self.assertIsInstance(native, ValueError)
        self.assertIsInstance(packed, self.gio.TransportError)
        self.assertNotIn(TOKEN, str(packed))

    def test_roots_are_the_gateways(self) -> None:
        self.assertEqual(self.core.API_ROOTS, self.gio.API_ROOTS)
        self.assertEqual(self.core.UPLOAD_ROOTS, self.gio.UPLOAD_ROOTS)
        self.assertEqual(self.core.TIMEOUT_S, self.gio.TIMEOUT_S)

    def test_the_payload_has_exactly_the_contract_keys(self) -> None:
        mp, kw = _CALLS[2]
        self.assertEqual(set(self.gio.pack_payload(TOKEN, self.req(mp, kw))),
                         set(self.core.ARGS))


# ══════════════════════════════════════════════════════════════════════════
# 6. the manifest through the gateway's parser (CLODIA_TOOLS_SRC)
# ══════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
class GatewayValidatorTests(unittest.TestCase):
    """google-pack's block through the gateway's own `manifest.py`, as the
    registry reads it with `connectors: {google: pack}` (google.yaml released)."""

    def setUp(self) -> None:
        conn = Path(TOOLS_SRC) / "server" / "connectors"
        self.m = _load(conn / "manifest.py", "gateway_manifest_524")
        # Standalone, `reserved_namespaces()` cannot reach `builtin.released()`;
        # «everything reserved» is its own fallback, i.e. the strictest view.
        p = patch.object(self.m, "reserved_namespaces",
                         lambda: self.m.MIGRATING_NAMESPACES)
        p.start()
        self.addCleanup(p.stop)
        self.builtins = {}
        for f in sorted((conn / "builtin_manifests").glob("*.yaml")):
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            self.builtins[f.stem] = doc
        self.google_builtin_file = conn / "builtin_manifests" / "google.yaml"

    def registry(self, *, released: tuple[str, ...], packs: dict):
        own = {d["name"]: d for stem, d in self.builtins.items() if stem not in released}
        return self.m.build_registry({**own, **packs}, builtin=tuple(own),
                                     reserved=self.m.MIGRATING_NAMESPACES)

    def test_the_fixture_is_the_checkouts_builtin(self) -> None:
        self.assertEqual(_builtin_doc(), yaml.safe_load(
            self.google_builtin_file.read_text(encoding="utf-8")),
            "refresh server/agents/fixtures/524_builtin_google.yaml (see its header)")

    def test_accepted_as_a_pack_manifest(self) -> None:
        cs = self.m.load_manifest(_pack_doc(), pack="google-pack",
                                  reserved=self.m.MIGRATING_NAMESPACES)
        self.assertEqual([c.id for c in cs], GOOGLE_IDS + ["mail-gmail"])

    def test_held_with_mail_native(self) -> None:
        reg = self.registry(released=("google",), packs={"google-pack": _pack_doc()})
        held = [c.id for c in reg.connectors if c.pack == "google-pack"]
        self.assertEqual(held, GOOGLE_IDS + ["mail-gmail"])
        (gmail,) = [p for p in reg.providers_of("mail") if p.pack == "google-pack"]
        self.assertEqual((gmail.id, gmail.transport.backend, gmail.transport.tool),
                         ("gmail", "gmail-transport", "run"))
        owner = reg.by_id("google")
        (ws,) = owner.providers
        self.assertEqual((ws.transport.backend, ws.transport.tool),
                         ("google-transport", "request"))

    def test_held_with_mail_on_comms_pack(self) -> None:
        comms = yaml.safe_load(Path(workspace_path(
            "catalogs/packs/comms-pack/pack.yaml")).read_text(encoding="utf-8"))
        # Only its mail vocabulary: Telegram's vault entry needs its own switch.
        comms["connectors"] = [_connector(comms, "mail")]
        reg = self.registry(released=("google", "mail"),
                            packs={"comms-pack": comms, "google-pack": _pack_doc()})
        self.assertEqual(sorted(p.id for p in reg.providers_of("mail")), ["gmail", "imap"])

    def test_the_pdp_reads_the_same_mappings(self) -> None:
        native = self.registry(released=(), packs={})
        packed = self.registry(released=("google",), packs={"google-pack": _pack_doc()})
        verbs = sorted({v["verb"] for c in _builtin_doc()["connectors"]
                        for v in c.get("verbs") or []})
        self.assertGreater(len(verbs), 10)
        for direction in ("egress", "source"):
            def shape(reg):
                out = {}
                for verb in verbs:
                    out[verb] = sorted(
                        (mp.scheme, mp.arg, mp.template, mp.dtype)
                        for _, mp in reg.mappings_for(verb, direction))
                return out
            self.assertEqual(shape(packed), shape(native), direction)
        self.assertEqual(
            sorted(p for p in packed.taint_prefixes() if p.startswith("gcalendar")),
            sorted(p for p in native.taint_prefixes() if p.startswith("gcalendar")))

    def test_canonical_answers_equal_the_builtins_when_elected(self) -> None:
        native = self.registry(released=(), packs={})
        packed = self.registry(released=("google",), packs={"google-pack": _pack_doc()})
        elected = {"google": ["drive.google.com", "docs.google.com"]}
        for uri in _URIS:
            with self.subTest(uri=uri):
                self.assertEqual(packed.canonical(uri, elected), native.canonical(uri))
        # Not elected: the pack never reads `https:`.
        self.assertIsNone(packed.canonical(
            "https://drive.google.com/drive/folders/X"))

    def test_refused_while_the_gateway_still_owns_google(self) -> None:
        # Native position: the builtin wins the collision, the pack's
        # connectors are not held — google-pack can be installed before the
        # switch is turned without taking anything over.
        try:
            reg = self.registry(released=(), packs={"google-pack": _pack_doc()})
        except self.m.ManifestError:
            return
        self.assertFalse([c for c in reg.connectors
                          if c.pack == "google-pack" and c.id in GOOGLE_IDS])


# ══════════════════════════════════════════════════════════════════════════
# 7. gmail-transport
# ══════════════════════════════════════════════════════════════════════════

#: A stand-in for email_client.py: reports what it sees, then behaves as asked.
_PROBE = textwrap.dedent(r"""
    import json, os, stat, sys, time
    root = os.getcwd()
    seen = {}
    for dirpath, dirs, files in os.walk(root):
        for d in dirs:
            p = os.path.join(dirpath, d)
            seen[os.path.relpath(p, root) + "/"] = oct(stat.S_IMODE(os.lstat(p).st_mode))
        for f in files:
            p = os.path.join(dirpath, f)
            seen[os.path.relpath(p, root)] = oct(stat.S_IMODE(os.lstat(p).st_mode))
    print(json.dumps({"argv": sys.argv[1:], "cwd": root, "files": seen,
                      "root_mode": oct(stat.S_IMODE(os.stat(root).st_mode)),
                      "env": dict(os.environ)}))
    sys.stdout.flush()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "fail":
        sys.exit(3)
    if mode == "sleep":
        time.sleep(30)
    if mode == "echo-secrets":
        d = os.environ["CLODIA_SECRETS_DIR"]
        for n in sorted(os.listdir(d)):
            sys.stderr.write(n + ": " + open(os.path.join(d, n)).read() + "\n")
        # Split so that no complete token sits in this source file.
        sys.stderr.write("minted: ya29" + ".A0minted-access-token_xyz and "
                         "Authorization: Bearer ya29" + ".other\n")
        sys.exit(1)
""")


def _oauth_files(account: str = "devnull", email: str = "devnull@example.org") -> dict:
    """The three files `vault.materialize_google_oauth` writes, as the gateway
    ships them (`secrets/<name>` → base64)."""
    return {
        "secrets/google_oauth_client.json": _b64(json.dumps(
            {"client_id": "123-fake.apps.googleusercontent.com",
             "client_secret": CLIENT_SECRET})),
        "secrets/email_oauth_tokens.json": _b64(json.dumps(
            {account: {"refresh_token": REFRESH, "email": email}})),
        "secrets/email_config.json": _b64(json.dumps({
            "accounts": {account: {"email": email, "auth": "oauth2",
                                   "imap_server": "127.0.0.1", "imap_port": 1,
                                   "smtp_server": "127.0.0.1", "smtp_port": 1}},
            "default": account})),
    }


class _IsolatedTmp(unittest.TestCase):
    """Every test gets its own TMPDIR, so "the dir was removed" is checkable."""

    def setUp(self) -> None:
        self.core = _mcore()
        self._box = tempfile.TemporaryDirectory()
        self.box = Path(self._box.name)
        self.tmp = self.box / "tmp"
        self.tmp.mkdir()
        p = patch.object(tempfile, "tempdir", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._box.cleanup)

    def leftovers(self) -> list[str]:
        return sorted(os.listdir(self.tmp))

    def with_probe(self):
        probe = self.box / "probe.py"
        probe.write_text(_PROBE, encoding="utf-8")
        return patch.object(self.core, "CLIENT", probe)


class GmailTransportIsCommsPacksTests(unittest.TestCase):
    """Same validation, files and placeholder code as comms-pack's transport."""

    SHARED = ("_check_relpath", "_check_request", "_write_files", "_remove",
              "_secret_values", "_trim", "_with_dir")
    CONSTANTS = ("DEFAULT_TIMEOUT", "MAX_TIMEOUT", "MAX_FILES", "MAX_FILES_BYTES",
                 "STDERR_LIMIT", "TIMEOUT_RETURNCODE", "DIR_PLACEHOLDER", "REDACTED",
                 "_SECRET_KEY_HINTS", "_MIN_SECRET_LEN")

    def setUp(self) -> None:
        self.gmail = _mcore()
        self.comms = _load(COMMS_MCP / "mail_transport_core.py", "mail_transport_core_524")

    def test_shared_functions_are_identical(self) -> None:
        for name in self.SHARED:
            with self.subTest(function=name):
                self.assertEqual(inspect.getsource(getattr(self.gmail, name)),
                                 inspect.getsource(getattr(self.comms, name)))

    def test_run_differs_only_by_name(self) -> None:
        mine = inspect.getsource(self.gmail.run).replace("gmail-transport", "mail-transport")
        self.assertEqual(mine, inspect.getsource(self.comms.run))

    def test_constants(self) -> None:
        for name in self.CONSTANTS:
            self.assertEqual(getattr(self.gmail, name), getattr(self.comms, name), name)

    def test_same_tool_schema(self) -> None:
        self.assertEqual(self.gmail.TOOL_SPECS[0]["inputSchema"],
                         self.comms.TOOL_SPECS[0]["inputSchema"])

    def test_the_bundled_client_is_comms_packs_copy(self) -> None:
        def body(path: Path) -> str:
            text = path.read_text(encoding="utf-8")
            start = text.index("Vendored into")
            end = text.index('"""', start)
            return text[:start] + text[end:]
        self.assertEqual(self.gmail.CLIENT, CLIENT.resolve())
        self.assertEqual(body(CLIENT), body(COMMS_MCP / "email_client.py"))
        self.assertIn("r-clodia/clodia-tools:vendor/email_client.py",
                      CLIENT.read_text(encoding="utf-8")[:1200])

    @unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
    def test_the_bundled_client_is_the_gateways_copy(self) -> None:
        text = CLIENT.read_text(encoding="utf-8")
        start = text.index("\nVendored into")
        end = text.index('"""', start)
        self.assertEqual(text[:start] + text[end:],
                         (Path(TOOLS_SRC) / "vendor" / "email_client.py")
                         .read_text(encoding="utf-8"))


class GmailRefusalTests(_IsolatedTmp):

    def _refused(self, rel: str) -> None:
        with self.with_probe():
            with self.assertRaises(self.core.TransportError, msg=rel):
                self.core.run({"argv": [], "files": {rel: _b64("x")}})
        self.assertEqual(self.leftovers(), [], rel)

    def test_absolute_and_traversal_paths(self) -> None:
        for rel in ("/etc/passwd", "C:/x", "..", "../x", "secrets/../../x", "./x",
                    "a//b", "a\\..\\x", "", "a\x00b"):
            self._refused(rel)

    def test_bad_types(self) -> None:
        with self.with_probe():
            for bad in ({"argv": "list"}, {"argv": [1]}, {"argv": [], "files": []},
                        {"argv": [], "files": {"a": "not base64!!"}},
                        {"argv": [], "timeout": 0}):
                with self.assertRaises(self.core.TransportError, msg=bad):
                    self.core.run(bad)
        self.assertEqual(self.leftovers(), [])


class GmailRunTests(_IsolatedTmp):

    def test_the_oauth_files_land_in_the_private_secrets_dir(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["--account", "devnull", "list"],
                                 "files": _oauth_files()})
        self.assertEqual(res["returncode"], 0, res)
        seen = json.loads(res["stdout"])
        self.assertEqual(seen["root_mode"], "0o700")
        for name in ("google_oauth_client.json", "email_oauth_tokens.json",
                     "email_config.json"):
            self.assertEqual(seen["files"][f"secrets/{name}"], "0o600")
        self.assertEqual(seen["env"]["CLODIA_SECRETS_DIR"],
                         os.path.join(seen["cwd"], "secrets"))
        self.assertTrue(Path(seen["cwd"]).name.startswith("gmail-transport-"))
        self.assertEqual(self.leftovers(), [])

    def test_dir_only_in_the_attachment_slot(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["ok", "--subject", "Offerta {dir}",
                                          "--body", "vedi {dir}/x",
                                          "--attachment", "{dir}/att/0/a.pdf"],
                                 "files": {"att/0/a.pdf": _b64("%PDF")}})
        seen = json.loads(res["stdout"])
        self.assertEqual(seen["argv"][2], "Offerta {dir}")
        self.assertEqual(seen["argv"][4], "vedi {dir}/x")
        self.assertEqual(seen["argv"][6], os.path.join(seen["cwd"], "att/0/a.pdf"))

    def test_dir_removed_on_failure_and_timeout(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["fail"], "files": _oauth_files()})
            self.assertEqual(res["returncode"], 3)
            res = self.core.run({"argv": ["sleep"], "files": _oauth_files(), "timeout": 1})
        self.assertEqual(res["returncode"], self.core.TIMEOUT_RETURNCODE)
        self.assertIn("gmail-transport: timed out", res["stderr"])
        self.assertEqual(self.leftovers(), [])

    def test_oauth_secrets_and_minted_tokens_are_scrubbed(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["echo-secrets"], "files": _oauth_files()})
        err = res["stderr"]
        for secret in (REFRESH, CLIENT_SECRET, MINTED, "ya29" + ".other"):
            self.assertNotIn(secret, err)
        self.assertIn(self.core.REDACTED, err)
        self.assertIn("email_oauth_tokens.json", err)   # the rest survives

    def test_calls_share_nothing(self) -> None:
        # Two concurrent calls, two directories: no file of one is visible to
        # the other, and nothing outlives either.
        outs: list = []

        def one(acc):
            outs.append(self.core.run({"argv": ["ok", acc],
                                       "files": _oauth_files(acc, f"{acc}@example.org")}))
        with self.with_probe():
            ts = [threading.Thread(target=one, args=(a,)) for a in ("uno", "due")]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        cwds = {json.loads(o["stdout"])["cwd"] for o in outs}
        self.assertEqual(len(cwds), 2)
        self.assertEqual(self.leftovers(), [])


class GmailRealClientTests(_IsolatedTmp):
    """The bundled client really runs on the XOAUTH2 files the gateway ships."""

    def test_reads_the_oauth_account_it_was_handed(self) -> None:
        res = self.core.run({"argv": ["accounts"], "files": _oauth_files()})
        self.assertEqual(res["returncode"], 0, res["stderr"])
        accounts = json.loads(res["stdout"])
        self.assertEqual(accounts[0]["email"], "devnull@example.org")
        self.assertNotIn(REFRESH, res["stdout"] + res["stderr"])
        self.assertNotIn(CLIENT_SECRET, res["stdout"] + res["stderr"])
        self.assertEqual(self.leftovers(), [])

    def test_connection_failure_does_not_echo_the_credentials(self) -> None:
        # IMAP on 127.0.0.1:1 fails before any token request: offline.
        res = self.core.run({"argv": ["--account", "devnull", "list"], "timeout": 30,
                             "files": _oauth_files()})
        self.assertNotEqual(res["returncode"], 0)
        self.assertNotIn(REFRESH, res["stdout"] + res["stderr"])
        self.assertNotIn(CLIENT_SECRET, res["stdout"] + res["stderr"])
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
