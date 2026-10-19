"""clodia-platform#558 — google-pack 2.0.0: Google runs in the gateway as a
component of the pack.

The pack (a) declares the four connectors of the gateway's builtin
`google.yaml` plus `mail-gmail`, and (b) ships a `target: gateway` component
(`gateway/`) that the GATEWAY fetches from this repository, pins and imports:
the 24 `gdrive/gsheets/gdocs/gcalendar` verbs, the folder/calendar perimeter,
the OAuth consent routes, Gmail, and the services the gateway calls. The
transports of 1.0.0 are gone. The agent-server side is the plugin with the
`topic-drive-sync` skill.

What could break silently, and is measured here:

1. **the declaration drifts from the builtin.** Any difference other than the
   declared ones (canonicalisation in the pack regex subset, `unmapped:`,
   `mail-gmail`) is the gate changing what the owner consented to. A copy of
   the builtin is vendored under `fixtures/`;
2. **the component drifts from the gateway.** Run here against a FAKE host
   (the component never imports the gateway): registration, perimeter reads,
   credential scopes, scratch I/O, routes, services. With `CLODIA_TOOLS_SRC`
   its `google_io` request framing is compared with the gateway's, function by
   function, its schemas with the gateway's native ones, and the gateway's own
   import guard scans it. (The real component inside the real gateway, with
   the Google suites re-run against it, is clodia-tools'
   `server/test_558_google_component.py` with `CLODIA_LOGIC_SRC`.)
3. **Gmail drifts.** `mail_run` (the gateway's `email.*` on `mail: native`)
   and `mail_accounts`/`mail_exec` (comms-pack's mail component on
   `mail: pack`, google-pack 2.1.0): the per-call directory, the `{dir}` rule,
   the XOAUTH2 files byte-identical to the gateway's
   `vault.materialize_google_oauth`, and the client is the gateway's copy.

With `CLODIA_TOOLS_SRC=<clodia-tools checkout>` the manifest also goes through
the gateway's own parser and registry. Without it those tests are skipped and
the rest runs offline.
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
GATEWAY = PACK / "gateway"
GMAIL_CORE = GATEWAY / "gmail_core.py"
CLIENT = GATEWAY / "email_client.py"
BASE_SKILLS = Path(workspace_path("catalogs/packs/base-pack/plugins/base-pack/skills"))
COMMS_GATEWAY = Path(workspace_path("catalogs/packs/comms-pack/gateway"))
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


def _mcore():
    return _load(GMAIL_CORE, "gmail_core_558")


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
                # Difference 3: `unmapped:` (#558), checked in UnmappedTests.
                pack.pop("unmapped", None)
                if cid == "google":
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
            "providers": [{"id": "gmail", "label": "Gmail"}]})

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
        self.assertEqual(self.pack["version"], "2.2.0")
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
# 2. the manifest: components, unmapped, the plugin
# ══════════════════════════════════════════════════════════════════════════

#: The 24 verbs, per namespace (the gateway's native catalogue, #558).
VERBS = {
    "gdrive": ["list", "search", "mkdir", "upload", "update", "download", "share",
               "rename", "move"],
    "gcalendar": ["list_calendars", "list_events", "create_event", "update_event",
                  "delete_event", "freebusy"],
    "gdocs": ["create", "read", "append_text", "replace_text"],
    "gsheets": ["list_tabs", "read", "add_tab", "append_rows", "write_range"],
}
ALL_VERBS = sorted(f"{ns}.{v}" for ns, vs in VERBS.items() for v in vs)


class ComponentsBlockTests(unittest.TestCase):
    def test_one_gateway_component_from_this_repository(self) -> None:
        self.assertEqual(_pack_doc()["components"], [{
            "id": "google", "target": "gateway",
            "source": {"repo": "r-clodia/clodia-logic",
                       "path": "catalogs/packs/google-pack/gateway", "ref": "main"},
            "entry": "google_component",
            "connectors": GOOGLE_IDS,
            "credentials": ["google_", "gworkspace_", "gmail_", "app_google_oauth"]}])

    def test_the_source_path_and_entry_exist(self) -> None:
        comp = _pack_doc()["components"][0]
        root = Path(workspace_path(comp["source"]["path"]))
        self.assertEqual(root, GATEWAY)
        self.assertTrue((root / f"{comp['entry']}.py").is_file())

    def test_the_installed_manifest_keeps_it(self) -> None:
        """The agent-server's install path rewrites `pack.yaml`; the gateway
        reads `first_party` and `components` from what it writes."""
        from ..api import pack_import
        with tempfile.TemporaryDirectory() as td, \
                patch.object(pack_import, "PACKS_META_DIR", Path(td) / "packs"), \
                patch.object(pack_import, "validate_connectors",
                             lambda c, pack: {"ok": True}), \
                patch.object(pack_import.plugin_import, "install_plugin_from_root",
                             lambda *a, **k: {"plugin": "google-pack"}):
            out = pack_import.install_pack_from_root(PACK, source="test",
                                                     allow_reserved=True)
            installed = yaml.safe_load((Path(td) / "packs" / "google-pack" / "pack.yaml")
                                       .read_text(encoding="utf-8"))
        self.assertIs(installed["first_party"], True)
        self.assertEqual(installed["components"], _pack_doc()["components"])
        self.assertEqual(installed["connectors"], _pack_doc()["connectors"])
        self.assertEqual(out["gateway_components"]["declared"], ["google"])
        self.assertEqual(out["gateway_components"]["refresh"],
                         "POST /internal/components/google-pack/refresh")


class UnmappedTests(unittest.TestCase):
    """Difference 3: every verb the builtin leaves without mapping or taint is
    listed in `unmapped:` of its connector — no more, no less."""

    def test_exactly_the_unclassified_verbs(self) -> None:
        builtin = _builtin_doc()
        mapped = {v["verb"] for c in builtin["connectors"] for v in c.get("verbs") or []}
        tainted = [t for c in builtin["connectors"] for t in c.get("taint") or []]
        want = sorted(v for v in ALL_VERBS
                      if v not in mapped and not any(v.startswith(t) for t in tainted))
        got = sorted(v for c in _pack_doc()["connectors"] for v in c.get("unmapped") or [])
        self.assertEqual(got, want)
        self.assertEqual(len(got), 11)

    def test_each_in_its_own_connectors_namespace(self) -> None:
        for c in _pack_doc()["connectors"]:
            for v in c.get("unmapped") or []:
                self.assertTrue(v.startswith(c["namespace"] + "."), (c["id"], v))


class PluginTests(unittest.TestCase):
    def test_no_transport_left(self) -> None:
        self.assertFalse((PLUGIN / ".mcp.json").exists())
        self.assertFalse((PLUGIN / "mcp").exists())
        for c in _pack_doc()["connectors"]:
            for prov in c.get("providers") or []:
                self.assertNotIn("transport", prov, (c["id"], prov["id"]))

    def test_the_drive_sync_skill_is_base_packs(self) -> None:
        mine = PLUGIN / "skills" / "topic-drive-sync" / "SKILL.md"
        self.assertEqual(mine.read_text(encoding="utf-8"),
                         (BASE_SKILLS / "topic-drive-sync" / "SKILL.md")
                         .read_text(encoding="utf-8"))

    def test_plugin_metadata(self) -> None:
        meta = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((meta["name"], meta["version"]), ("google-pack", "2.0.0"))


# ══════════════════════════════════════════════════════════════════════════
# 3. the component, on a fake host
# ══════════════════════════════════════════════════════════════════════════

PKG = "gpc558"


def _load_component():
    """Import `gateway/` as a package of its own, without writing bytecode
    into the repository (the gateway refuses a tree with `__pycache__`)."""
    for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
        sys.modules.pop(k, None)
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(GATEWAY)]
    pkg.__spec__ = importlib.util.spec_from_loader(PKG, loader=None, is_package=True)
    pkg.__spec__.submodule_search_locations = [str(GATEWAY)]
    sys.modules[PKG] = pkg
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import importlib as _il
        return _il.import_module(f"{PKG}.google_component")
    finally:
        sys.dont_write_bytecode = before


def _mod(name: str):
    return sys.modules[f"{PKG}.{name}"]


class FakeCredentials:
    def __init__(self, host, prefix):
        self.host, self.prefix = host, prefix

    def _match(self, name):
        if self.prefix.endswith("_"):
            return name.startswith(self.prefix) and len(name) > len(self.prefix)
        return name == self.prefix

    def _check(self, name):
        if not self._match(name):
            raise PermissionError(f"{name} outside {self.prefix}")

    def list(self):
        return [n for n in self.host.vault if self._match(n)]

    def has(self, name):
        self._check(name)
        return name in self.host.vault

    def read_internal(self, name):
        self._check(name)
        self.host.reads.append(name)
        if name not in self.host.vault:
            raise PermissionError(f"absent: {name}")
        return dict(self.host.vault[name])

    def deposit(self, name, bundle, *, cred_type="opaque", grant_agents=()):
        self._check(name)
        self.host.vault[name] = dict(bundle)
        self.host.deposits.append((name, dict(bundle), cred_type, list(grant_agents)))


class FakePerimeter:
    def __init__(self):
        self.roots: dict = {}
        self.egress: list[str] = []
        self.topics: dict = {}
        self.channel_flag = False

    def gdrive_roots(self):
        return copy.deepcopy(self.roots)

    def egress_uris(self, scope=None):
        return list(self.egress)

    def approved_calendars(self, scope=None):
        return [u[len("gcal:"):] for u in self.egress if u.startswith("gcal:")]

    def calendar_allowed(self, calendar_id, scope=None):
        cid = (calendar_id or "").lower()
        for a in self.approved_calendars():
            a = a.lower()
            if a == cid or (a.startswith("*@") and cid.endswith(a[1:])):
                return True
        return False

    def topic_drive_folders(self, tier, name):
        return [{"folder": f} for f in self.topics.get((tier, name), [])]

    def in_channel(self):
        return self.channel_flag


class FakeHost:
    """What `server.components.host.Host` offers, recorded."""

    def __init__(self, scratch: str):
        import logging
        self.verbs, self.routes, self.services = {}, {}, {}
        self.vault: dict[str, dict] = {}
        self.reads: list[str] = []
        self.deposits: list = []
        self.crossings: list = []
        self.perimeter = FakePerimeter()
        self.scratch = scratch
        self.chan = None
        self.switch_native = False
        self.log = logging.getLogger("fake-host")

    def register_verb(self, tool, handler, *, offload=True):
        assert tool.name not in self.verbs
        self.verbs[tool.name] = (tool, handler)

    def register_route(self, path, methods, handler, *, auth="owner"):
        assert auth in ("owner", "admin")
        self.routes[path] = (frozenset(methods), handler, auth)

    def register_service(self, name, handler):
        self.services[name] = handler

    def credentials(self, prefix):
        if self.switch_native:
            raise PermissionError("switch on native")
        return FakeCredentials(self, prefix)

    def channel(self):
        return self.chan

    def scratch_path(self, path):
        full = os.path.realpath(os.path.join(self.scratch, path))
        if not full.startswith(os.path.realpath(self.scratch) + os.sep):
            raise PermissionError("outside the scratch")
        return full

    def note_crossing(self, data=None, *, path=None, kind="file"):
        self.crossings.append((path, kind))


class FakeDrive:
    """A `google_io._transport` answering from a parents table."""

    def __init__(self, tree, *, mimes=None, shortcuts=()):
        self.tree, self.mimes, self.shortcuts = dict(tree), dict(mimes or {}), set(shortcuts)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, account, req):
        self.calls.append((req["method"], req["template"],
                           {**req["path_params"], **req["params"]}))
        key = (req["method"], req["template"])
        if key == ("GET", "files/{fileId}"):
            fid = req["path_params"]["fileId"]
            if fid not in self.tree:
                raise RuntimeError("404")
            if req["download"]:
                return b"BYTES-" + fid.encode()
            out = {"id": fid, "name": f"n-{fid}", "parents": list(self.tree[fid]),
                   "mimeType": self.mimes.get(fid, "text/plain")}
            if fid in self.shortcuts:
                out["shortcutDetails"] = {"targetId": "x"}
            return out
        if key == ("GET", "files"):
            return {"files": [{"id": f, "name": f, "parents": p, "mimeType": "text/plain",
                               **({"shortcutDetails": {"targetId": "x"}}
                                  if f in self.shortcuts else {})}
                              for f, p in self.tree.items()]}
        if key in (("POST", "files"), ("PATCH", "files/{fileId}")):
            return {"id": req["path_params"].get("fileId", "NEW"), "name": "x"}
        if key == ("GET", "users/me/calendarList"):
            return {"items": [{"id": "cal@x.it", "summary": "A", "primary": True},
                              {"id": "other@y.it", "summary": "B"}]}
        if key == ("GET", "calendars/{calendarId}/events"):
            return {"items": []}
        raise AssertionError(f"unexpected {key}")


class _OnFakeHost(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.host = FakeHost(self.tmp)
        before = sys.dont_write_bytecode
        sys.dont_write_bytecode = True        # `register` imports the modules
        try:
            self.comp = _load_component()
            self.comp.register(self.host)
        finally:
            sys.dont_write_bytecode = before
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == PKG or k.startswith(PKG + ".")])
        self.host.vault["google_conto"] = {"client_id": "CID", "client_secret": CLIENT_SECRET,
                                           "refresh_token": REFRESH, "email": "c@x.it"}
        self.gio = _mod("google_io")
        self.real_access_token = self.gio.access_token
        p = patch.object(self.gio, "access_token", lambda a: TOKEN)
        p.start()
        self.addCleanup(p.stop)

    def drive(self, tree, **kw) -> FakeDrive:
        fake = FakeDrive(tree, **kw)
        p = patch.object(self.gio, "_transport", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def call(self, verb, args):
        return self.host.verbs[verb][1](args)


class RegisterTests(_OnFakeHost):
    def test_the_24_verbs_routes_and_services(self) -> None:
        self.assertEqual(sorted(self.host.verbs), ALL_VERBS)
        self.assertEqual({p: (sorted(m), a) for p, (m, _h, a) in self.host.routes.items()},
                         {"app": (["GET", "POST"], "owner"), "auth": (["GET"], "admin"),
                          "connect": (["POST"], "owner")})
        self.assertEqual(sorted(self.host.services), sorted([
            "accounts", "describe_folder", "perimeter_changed", "credential_changed",
            "folder_labels", "pdp_arguments", "test_connection", "mail_run",
            "mail_accounts", "mail_exec"]))
        self.assertIs(self.host.services["mail_run"], _mod("gmail_core").run)

    def test_no_import_of_the_gateway(self) -> None:
        """The rule of the gateway's import guard (advisory there, by
        construction here): nothing under `gateway/` imports `server`."""
        for path in sorted(GATEWAY.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(
                        node.func, "id", "")) in ("import_module", "__import__"):
                    self.fail(f"{path.name}: dynamic import")
                else:
                    continue
                for n in names:
                    with self.subTest(file=path.name, module=n):
                        self.assertFalse(n == "server" or n.startswith("server."))



class CredentialTests(_OnFakeHost):
    def test_accounts_and_the_preferred_bundle(self) -> None:
        self.host.vault["gworkspace_vecchio"] = {"x": 1}
        self.host.vault["mailbox_info"] = {"x": 1}
        gd = _mod("gdrive")
        self.assertEqual(gd.gworkspace_accounts(), ["conto", "vecchio"])
        self.assertEqual(self.host.services["accounts"](), ["conto", "vecchio"])
        self.assertEqual(gd._drive_cred("conto"), "google_conto")
        self.assertEqual(gd._drive_cred("vecchio"), "gworkspace_vecchio")

    def test_the_token_is_refreshed_from_the_vault_through_the_host(self) -> None:
        gio = _mod("google_io")
        gio.forget()
        seen = []
        with patch.object(gio, "access_token", self.real_access_token), \
                patch.object(gio, "_refresh", lambda b: seen.append(b) or ("ya29.T", 3600)):  # gitleaks:allow — fake test credential
            self.assertEqual(gio.access_token("conto"), "ya29.T")  # gitleaks:allow — fake test credential
            self.assertEqual(gio.access_token("conto"), "ya29.T")      # gitleaks:allow — cached
        self.assertEqual(self.host.reads, ["google_conto"])
        self.assertEqual(seen[0]["refresh_token"], REFRESH)
        self.host.services["credential_changed"]("google_conto")
        self.assertEqual(gio._tokens, {})

    def test_no_credential_when_the_switch_is_native(self) -> None:
        self.host.switch_native = True
        with self.assertRaises(PermissionError):
            _mod("gdrive").gworkspace_accounts()


class PerimeterTests(_OnFakeHost):
    TREE = {"ROOT": ["MY"], "MY": [], "in": ["ROOT"], "deep": ["in"], "out": ["ELSE"],
            "ELSE": [], "SC": ["ROOT"], "TOPIC": ["MY"], "t1": ["TOPIC"]}

    def setUp(self) -> None:
        super().setUp()
        self.fake = self.drive(self.TREE, shortcuts={"SC"})
        gr = _mod("gdrive_root")
        gr.reset_cache()
        gr.reset_topic_cache()

    def test_account_root_from_the_gateway_config(self) -> None:
        self.host.perimeter.roots = {"conto": ["ROOT"]}
        out = self.call("gdrive.rename", {"file_id": "deep", "new_name": "x"})
        self.assertTrue(out["renamed"])
        for fid in ("out", "SC"):
            with self.assertRaises(PermissionError):
                self.call("gdrive.rename", {"file_id": fid, "new_name": "x"})
        listed = self.call("gdrive.list", {})
        self.assertEqual(sorted(f["id"] for f in listed["files"]), ["ROOT", "deep", "in"])

    def test_the_topic_perimeter_comes_from_the_channel(self) -> None:
        self.host.chan = "SEAL-1/acme"
        self.host.perimeter.channel_flag = True
        self.host.perimeter.topics[("SEAL-1", "acme")] = ["TOPIC"]
        gr = _mod("gdrive_root")
        self.assertEqual(gr.roots_for_call("conto"), (["TOPIC"], "topic"))
        self.call("gdrive.rename", {"file_id": "t1", "new_name": "x"})
        with self.assertRaises(PermissionError):
            self.call("gdrive.rename", {"file_id": "in", "new_name": "x"})

    def test_approved_folders_come_from_the_egress_list(self) -> None:
        self.host.perimeter.egress = ["gdrive:folder/ROOT", "mailto:a@b.it"]
        self.assertEqual(_mod("gdrive_root").approved_folders(), ["ROOT"])
        self.assertTrue(_mod("gdrive_root").confined("conto"))

    def test_the_calendar_needs_a_gcal_approval_when_confined(self) -> None:
        self.host.perimeter.roots = {"conto": ["ROOT"]}
        with self.assertRaises(PermissionError):
            self.call("gcalendar.list_events", {"calendar_id": "cal@x.it"})
        self.host.perimeter.egress = ["gcal:cal@x.it"]
        self.assertEqual(self.call("gcalendar.list_events",
                                   {"calendar_id": "cal@x.it"})["events"], [])
        out = self.call("gcalendar.list_calendars", {})
        self.assertEqual([c["id"] for c in out["calendars"]], ["cal@x.it"])

    def test_perimeter_changed_drops_the_caches(self) -> None:
        gr = _mod("gdrive_root")
        gr._parents_cache[("conto", "x")] = (10**12, (), False)
        self.host.services["perimeter_changed"]()
        self.assertEqual(gr._parents_cache, {})


class VerbTests(_OnFakeHost):
    def test_upload_reads_the_scratch_through_the_host(self) -> None:
        self.drive({"F": []})
        Path(self.tmp, "a.txt").write_text("x")
        out = self.call("gdrive.upload", {"src": "a.txt", "folder_id": "F"})
        self.assertTrue(out["uploaded"])
        self.assertEqual(self.host.crossings,
                         [(os.path.realpath(os.path.join(self.tmp, "a.txt")), "upload")])
        with self.assertRaises(PermissionError):
            self.call("gdrive.upload", {"src": "../../etc/passwd"})

    def test_download_writes_into_the_scratch(self) -> None:
        self.drive({"F1": []})
        out = self.call("gdrive.download", {"file_id": "F1", "dest": "sub/f.bin"})
        self.assertEqual(Path(out["local_path"]).read_bytes(), b"BYTES-F1")
        self.assertTrue(out["local_path"].startswith(os.path.realpath(self.tmp)))

    def test_pdp_arguments(self) -> None:
        fake = self.drive({"DOC": ["PARENT"], "PARENT": []})
        svc = self.host.services["pdp_arguments"]
        self.assertEqual(svc("gdrive.update", {"file_id": "DOC"}), {"folder_id": "PARENT"})
        self.assertEqual(fake.calls[-1][2], {"fileId": "DOC", "fields": "parents",
                                             "supportsAllDrives": True})
        self.assertEqual(svc("gdrive.update", {"file_id": "MISSING"}), {"folder_id": ""})
        self.assertEqual(svc("gdrive.list", {}), {})

    def test_describe_folder(self) -> None:
        self.drive({"F": []}, mimes={"F": "application/vnd.google-apps.folder"})
        self.assertEqual(self.host.services["describe_folder"]("conto", "F"),
                         {"id": "F", "name": "n-F", "is_folder": True})
        out = self.host.services["describe_folder"]("conto", "NOPE")
        self.assertFalse(out["is_folder"])
        self.assertIn("error", out)


class _Req:
    def __init__(self, method="GET", body=None):
        self.method = method
        self._body = body or {}

    async def json(self):
        return self._body


class RouteTests(_OnFakeHost):
    APP = {"client_id": "CID", "client_secret": CLIENT_SECRET,
           "redirect_uri": "http://127.0.0.1"}

    def route(self, path, method="GET", body=None):
        import asyncio
        r = asyncio.run(self.host.routes[path][1](_Req(method, body)))
        return r.status_code, json.loads(r.body)

    def test_app_status_and_config(self) -> None:
        self.assertEqual(self.route("app"), (200, {"configured": False,
                                                    "redirect_uri": "http://127.0.0.1"}))
        self.assertEqual(self.route("app", "POST", {"client_id": "N"})[0], 400)
        code, _ = self.route("app", "POST", {"client_json": json.dumps(
            {"installed": {"client_id": "N", "client_secret": "S",  # gitleaks:allow
                           "redirect_uris": ["http://localhost"]}})})
        self.assertEqual(code, 200)
        self.assertEqual(self.host.deposits[-1], (
            "app_google_oauth", {"client_id": "N", "client_secret": "S",  # gitleaks:allow
                                 "redirect_uri": "http://localhost"}, "google_app", []))

    def test_consent_and_custody(self) -> None:
        self.assertEqual(self.route("auth")[0], 409)
        self.host.vault["app_google_oauth"] = dict(self.APP)
        code, body = self.route("auth")
        self.assertEqual(code, 200)
        self.assertIn("client_id=CID", body["auth_url"])
        self.assertIn("select_account+consent", body["auth_url"])
        state = body["state"]
        self.assertEqual(self.route("connect", "POST", {"code": "C", "state": "bogus"})[0], 400)
        go = _mod("google_oauth")
        with patch.object(go, "exchange_code",  # gitleaks:allow — fake test credential
                          lambda *a: {"refresh_token": "RT", "access_token": "AT"}), \
                patch.object(go, "get_profile_email", lambda t: "Mario.Rossi@x.it"):
            code, body = self.route("connect", "POST",
                                    {"code": "http://127.0.0.1/?code=C&x=1", "state": state})
        self.assertEqual((code, body), (200, {"connected": True, "account": "Mario_Rossi",
                                              "email": "Mario.Rossi@x.it"}))
        self.assertEqual(self.host.deposits[-1], (
            "google_Mario_Rossi",
            {"client_id": "CID", "client_secret": CLIENT_SECRET, "refresh_token": "RT",  # gitleaks:allow — fake test credential
             "email": "Mario.Rossi@x.it", "account": "Mario_Rossi",
             "scope": go.UNIFIED_SCOPE},
            "oauth2_google", ["clodia"]))
        # The state is single use.
        self.assertEqual(self.route("connect", "POST", {"code": "C", "state": state})[0], 400)


class TestConnectionTests(_OnFakeHost):
    def test_no_account(self) -> None:
        self.host.vault.clear()
        self.assertEqual(self.host.services["test_connection"](),
                         {"ok": None, "detail": "nessun account Google connesso"})

    def test_ok_and_revoked(self) -> None:
        routes = _mod("routes")

        class _R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def ok(req, timeout):
            if req.full_url == routes.go.TOKEN_URL:
                return _R(json.dumps({"access_token": "ya29.x"}).encode())  # gitleaks:allow — fake test credential
            self.assertEqual(req.headers["Authorization"], "Bearer ya29.x")  # gitleaks:allow — fake test credential
            return _R(json.dumps({"email": "c@x.it"}).encode())
        with patch.object(self.gio, "_open", ok):
            self.assertEqual(self.host.services["test_connection"](),
                             {"ok": True, "detail": "conto: ok (c@x.it)"})

        def revoked(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                         io.BytesIO(b'{"error": "invalid_grant"}'))
        with patch.object(self.gio, "_open", revoked):
            out = self.host.services["test_connection"]()
        self.assertFalse(out["ok"])
        self.assertIn("riconnetti", out["detail"])
        self.assertNotIn(REFRESH, out["detail"])


# ══════════════════════════════════════════════════════════════════════════
# 4. the component against the gateway's sources (CLODIA_TOOLS_SRC)
# ══════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
class GatewaySourcesTests(unittest.TestCase):
    """The moved code is the gateway's code: same REST framing, same schemas,
    and the gateway's own import guard finds nothing."""

    #: Functions of `google_io` that frame the wire request or refresh a token.
    FRAMING = ("request", "_http_transport", "_query", "_multipart", "_send", "_reason",
               "_NoTokenAcrossHosts", "_refresh", "forget")

    def _gateway_module(self, rel: str, name: str):
        root = Path(TOOLS_SRC) / "server"
        return _load(root / rel, name)

    def test_google_io_framing_is_the_gateways(self) -> None:
        native = Path(TOOLS_SRC) / "server" / "tools" / "google_io.py"
        if not native.is_file():
            self.skipTest("the gateway no longer has a Google client (clodia-tools "
                          "≥ 2.121.0, #524): the component's is the only copy")
        gw = ast.parse(native.read_text(encoding="utf-8"))
        mine = ast.parse((GATEWAY / "google_io.py").read_text(encoding="utf-8"))

        def defs(tree):
            return {n.name: ast.dump(n) for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        g, m = defs(gw), defs(mine)
        for name in self.FRAMING + ("HttpError", "RefreshError", "Media"):
            with self.subTest(name=name):
                self.assertEqual(m[name], g[name])
        consts = ("API_ROOTS", "UPLOAD_ROOTS", "TOKEN_URL", "TIMEOUT_S", "TOKEN_MARGIN_S")

        def assigns(tree):
            return {t.id: ast.dump(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        ga, ma = assigns(gw), assigns(mine)
        for name in consts:
            self.assertEqual(ma[name], ga[name], name)

    def test_the_schemas_are_the_gateways(self) -> None:
        from mcp.types import Tool
        src = (Path(TOOLS_SRC) / "server" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        ns: dict = {"Tool": Tool}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") in (
                    "_GDRIVE_TOOLS", "_GCALENDAR_TOOLS", "_GDOCS_TOOLS", "_GSHEETS_TOOLS"):
                exec(f"{node.target.id} = {ast.get_source_segment(src, node.value)}", ns)
        if "_GDRIVE_TOOLS" not in ns:
            self.skipTest("the gateway no longer declares the Google verbs (clodia-tools "
                          "≥ 2.121.0, #524): these schemas are the only copy")
        native = (ns["_GDRIVE_TOOLS"] + ns["_GCALENDAR_TOOLS"] + ns["_GDOCS_TOOLS"]
                  + ns["_GSHEETS_TOOLS"])
        _load_component()
        before = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            import importlib as _il
            mine = _il.import_module(f"{PKG}.schemas").TOOLS
        finally:
            sys.dont_write_bytecode = before
        def shape(t):
            # `inputSchema` in the gateway's mcp 1.x, `input_schema` in 2.x.
            return (t.name, t.description,
                    getattr(t, "inputSchema", None) or getattr(t, "input_schema", None))
        self.assertEqual([shape(t) for t in mine], [shape(t) for t in native])
        self.assertTrue(all(shape(t)[2] for t in mine))

    def test_the_gateways_import_guard_finds_nothing(self) -> None:
        root = Path(TOOLS_SRC)
        sys.path.insert(0, str(root))
        try:
            guard = _load(root / "server" / "components" / "guard.py", "guard_558")
        finally:
            sys.path.remove(str(root))
        with tempfile.TemporaryDirectory() as td:
            import shutil
            dst = Path(td) / "gateway"
            shutil.copytree(GATEWAY, dst, ignore=shutil.ignore_patterns("__pycache__"))
            self.assertEqual(guard.scan_tree(dst), [])

# ══════════════════════════════════════════════════════════════════════════
# 5. the manifest through the gateway's parser (CLODIA_TOOLS_SRC)
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
        # Since clodia-tools 2.124.0 (clodia-platform#526) the gateway ships
        # only `core`/`datastore`; its former declarations (mail, google, …)
        # are kept as the parity reference in `testdata/former_builtin/`.
        former = conn / "testdata" / "former_builtin"
        dirs = [conn / "builtin_manifests"] + ([former] if former.is_dir() else [])
        for d in dirs:
            for f in sorted(d.glob("*.yaml")):
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
                self.builtins[f.stem] = doc
        self.google_builtin_file = next(
            d / "google.yaml" for d in reversed(dirs) if (d / "google.yaml").is_file())

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
        # No transport: Gmail is the gateway component's `mail_run` service.
        self.assertEqual((gmail.id, gmail.transport), ("gmail", None))
        owner = reg.by_id("google")
        (ws,) = owner.providers
        self.assertIsNone(ws.transport)

    def test_unmapped_is_parsed(self) -> None:
        if not hasattr(self.m.Connector, "__dataclass_fields__") or \
                "unmapped_verbs" not in self.m.Connector.__dataclass_fields__:
            self.skipTest("gateway checkout older than 2.109.0 (no `unmapped:`)")
        reg = self.registry(released=("google",), packs={"google-pack": _pack_doc()})
        self.assertEqual(sorted(reg.unmapped_verbs()),
                         sorted(v for c in _pack_doc()["connectors"]
                                for v in c.get("unmapped") or []))

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
# 6. Gmail (`gmail_core.py`, the `mail_run` service)
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


class GmailClientIsCommsPacksTests(unittest.TestCase):
    """The bundled client is the same as comms-pack's gateway component's (and,
    with CLODIA_TOOLS_SRC, the gateway's own) — except for the vendoring note.
    (Until comms-pack 1.4.0 `gmail_core` also mirrored comms-pack's transport
    function by function; the transport is gone since comms-pack 2.0.0.)"""

    @staticmethod
    def _body(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        start = text.index("\nVendored into")
        end = text.index('"""', start)
        return text[:start] + text[end:]

    def test_the_bundled_client_is_comms_packs_copy(self) -> None:
        self.assertEqual(_mcore().CLIENT, CLIENT.resolve())
        self.assertEqual(self._body(CLIENT), self._body(COMMS_GATEWAY / "email_client.py"))
        self.assertIn("r-clodia/clodia-tools:vendor/email_client.py",
                      CLIENT.read_text(encoding="utf-8")[:1200])

    @unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
    def test_the_bundled_client_is_the_gateways_copy(self) -> None:
        gw = Path(TOOLS_SRC) / "vendor" / "email_client.py"
        if not gw.is_file():
            self.skipTest("the gateway no longer ships a mail client (clodia-tools "
                          "≥ 2.120.0, #523)")
        self.assertEqual(self._body(CLIENT), gw.read_text(encoding="utf-8"))


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
        self.assertTrue(Path(seen["cwd"]).name.startswith("gmail-component-"))
        # Nothing may write bytecode next to the pinned component.
        self.assertEqual(seen["env"]["PYTHONDONTWRITEBYTECODE"], "1")
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
        self.assertIn("gmail: timed out", res["stderr"])
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


class GmailForCommsPackTests(_OnFakeHost):
    """`mail_accounts` and `mail_exec`: what comms-pack's mail component asks
    (google-pack 2.1.0). Addresses out, never a secret; the XOAUTH2 files are
    written HERE from this component's own credential."""

    def test_mail_accounts_rows_carry_no_secret(self) -> None:
        self.host.vault["gmail_vecchio"] = {"email": "v@x.it"}      # incomplete
        rows = self.host.services["mail_accounts"]()
        self.assertEqual(rows, [
            {"credential": "google_conto", "account": "conto", "kind": "google",
             "operational": True, "send_only": False, "missing": [], "error": None,
             "email": "c@x.it"},
            {"credential": "gmail_vecchio", "account": "vecchio", "kind": "gmail",
             "operational": False, "send_only": False,
             "missing": ["client_id", "client_secret", "refresh_token"], "error": None,
             "email": "v@x.it"}])
        blob = json.dumps(rows)
        for secret in (REFRESH, CLIENT_SECRET, "CID"):
            self.assertNotIn(secret, blob)

    def test_mail_exec_writes_the_gateways_files_itself(self) -> None:
        core = _mod("gmail_core")
        box = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(box, True))
        probe = Path(box) / "probe.py"
        probe.write_text(textwrap.dedent(r"""
            import json, os, sys
            d = os.environ["CLODIA_SECRETS_DIR"]
            files = {n: open(os.path.join(d, n)).read() for n in sorted(os.listdir(d))}
            print(json.dumps({"argv": sys.argv[1:], "files": files}))
        """), encoding="utf-8")
        with patch.object(core, "CLIENT", probe):
            res = self.host.services["mail_exec"]({
                "account": "conto", "argv": ["send", "--attachment", "{dir}/att/0/a.pdf"],
                "files": {"att/0/a.pdf": _b64("%PDF")}, "timeout": 30})
        self.assertEqual(res["returncode"], 0, res)
        seen = json.loads(res["stdout"])
        self.assertEqual(seen["argv"][:3], ["--account", "conto", "send"])
        self.assertTrue(seen["argv"][4].endswith("/att/0/a.pdf"))
        # Byte for byte what the gateway's `vault.materialize_google_oauth` writes.
        account = "c"     # `account` absent from the bundle: the email's local part
        self.assertEqual(seen["files"], {
            "google_oauth_client.json": json.dumps(
                {"client_id": "CID", "client_secret": CLIENT_SECRET}),
            "email_oauth_tokens.json": json.dumps(
                {account: {"refresh_token": REFRESH, "email": "c@x.it"}}),
            "email_config.json": json.dumps({
                "accounts": {account: {"email": "c@x.it", "auth": "oauth2",
                                       "imap_server": "imap.gmail.com", "imap_port": 993,
                                       "smtp_server": "smtp.gmail.com", "smtp_port": 587}},
                "default": account}),
        })

    def test_mail_exec_takes_attachments_only(self) -> None:
        core = _mod("gmail_core")
        for files in ({"secrets/email_config.json": _b64("{}")}, {"x": _b64("1")},
                      {"../att/x": _b64("1")}):
            with self.subTest(files=list(files)):
                with self.assertRaises(core.TransportError):
                    self.host.services["mail_exec"]({"account": "conto", "argv": [],
                                                     "files": files})

    def test_mail_exec_runs_only_mail_commands_and_att_attachments(self) -> None:
        """Review of clodia-tools#384: a caller could make the client attach
        the call's own OAuth files (`secrets/…`)."""
        core = _mod("gmail_core")
        for argv in (["configure"], ["send", "--to", "x@y.it", "--attachment",
                                     "secrets/email_oauth_tokens.json"],
                     ["send", "--attachment=secrets/email_config.json"],
                     ["send", "--attachment", "{dir}/att/../secrets/x"],
                     ["download-attachments", "1"]):
            with self.subTest(argv=argv):
                with self.assertRaises(core.TransportError):
                    self.host.services["mail_exec"]({"account": "conto", "argv": argv})

    def test_mail_exec_refuses_an_unknown_or_bad_account(self) -> None:
        core = _mod("gmail_core")
        with self.assertRaises(PermissionError):
            self.host.services["mail_exec"]({"account": "altro", "argv": []})
        for bad in ("", None, 3):
            with self.assertRaises(core.TransportError):
                self.host.services["mail_exec"]({"account": bad, "argv": []})

    @unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
    def test_the_gmail_servers_are_the_gateways(self) -> None:
        src = (Path(TOOLS_SRC) / "server" / "vault.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        (node,) = [n for n in tree.body if isinstance(n, ast.Assign)
                   and getattr(n.targets[0], "id", "") == "_GMAIL_SERVERS"]
        self.assertEqual(ast.literal_eval(node.value),
                         _mod("gmail_accounts")._GMAIL_SERVERS)


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
