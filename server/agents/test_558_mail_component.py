"""clodia-platform#558 — comms-pack 2.0.0: mail runs in the gateway as a
component of the pack.

The pack (a) declares the `mail` connector — the gateway's builtin `mail.yaml`,
moved — and (b) ships a `target: gateway` component (`gateway/`) that the
GATEWAY fetches from this repository, pins and imports: the eight `email.*`
verbs and every decision they carry (account, mailbox whitelist, strict
ingress with its audit record, topic attachments, reply recipient), the
mailbox management of the Tools page, the IMAP/SMTP client. The
`mail-transport` backend of 1.4.0 is gone. Google accounts are google-pack's,
asked through its component's services.

What could break silently, and is measured here:

1. **the declaration drifts from the builtin.** Any difference other than the
   three declared additions (`forms:` on inbox/outbox, the `imap` provider,
   `unmapped:`) is the gate changing what the owner consented to. A copy of
   the builtin is vendored under `fixtures/`;
2. **the component drifts from the gateway.** Run here against a FAKE host
   (the component never imports the gateway): registration, the client run on
   one mailbox's credential, whitelist and strict ingress through the host's
   perimeter, Gmail through google-pack's services, topic attachments, the
   reply recipient, the mailbox services. With `CLODIA_TOOLS_SRC` its schemas
   are compared with the gateway's, its client with the gateway's vendored
   copy, and the gateway's own import guard scans it. (The real component
   inside the real gateway, with the mail suites re-run against it, is
   clodia-tools' `server/test_558_mail_component.py` with `CLODIA_LOGIC_SRC`.)
"""
from __future__ import annotations

import ast
import base64
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ..config import workspace_path

PACK = Path(workspace_path("catalogs/packs/comms-pack"))
PLUGIN = PACK / "plugins" / "comms-pack"
GATEWAY = PACK / "gateway"
CLIENT = GATEWAY / "email_client.py"
GOOGLE_CLIENT = Path(workspace_path("catalogs/packs/google-pack/gateway/email_client.py"))
BUILTIN = Path(__file__).resolve().parent / "fixtures" / "523_builtin_mail.yaml"
TOOLS_SRC = os.environ.get("CLODIA_TOOLS_SRC", "")

SECRET = "S3cr3t-Pa55w0rd-558-xyz"  # gitleaks:allow — fake test credential
EMAIL_VERBS = sorted(["email.send", "email.folders", "email.list", "email.read",
                      "email.get_attachment", "email.save_attachment", "email.search",
                      "email.reply"])
SERVICES = sorted(["pdp_arguments", "diagnostics", "system_mailboxes", "mailbox_address",
                   "mailboxes_list", "mailbox_add", "mailbox_remove", "test_mailboxes"])


def _connector(doc: dict, cid: str = "mail") -> dict:
    for entry in doc.get("connectors") or []:
        if isinstance(entry, dict) and entry.get("id") == cid:
            return entry
    raise AssertionError(f"no connector '{cid}'")


def _pack_doc() -> dict:
    return yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ══════════════════════════════════════════════════════════════════════════
# 1. the declaration
# ══════════════════════════════════════════════════════════════════════════

class DeclarationMatchesTheBuiltinTests(unittest.TestCase):
    """The pack's `mail` is the gateway's builtin plus exactly three additions."""

    def setUp(self) -> None:
        self.pack = _connector(_pack_doc())
        self.builtin = _connector(yaml.safe_load(BUILTIN.read_text(encoding="utf-8")))

    def test_same_connector_except_the_three_additions(self) -> None:
        pack = json.loads(json.dumps(self.pack))
        pack.pop("providers")
        pack.pop("unmapped")
        for scheme in pack["schemes"]:
            if scheme["scheme"] in ("inbox", "outbox"):
                scheme.pop("forms")
        self.assertEqual(pack, self.builtin)

    def test_every_field_compared_is_really_there(self) -> None:
        for key in ("schemes", "verbs", "topic_actions", "tools_card", "taint"):
            self.assertTrue(self.builtin.get(key), key)
        self.assertEqual(self.pack["namespace"], "email")
        action = self.pack["topic_actions"][0]
        self.assertTrue(action["admin_only"])
        self.assertEqual(action["fields"][0]["options"], "mailboxes")

    def test_forms_only_on_inbox_and_outbox_and_they_accept_an_address(self) -> None:
        import re
        with_forms = {s["scheme"] for s in self.pack["schemes"] if s.get("forms")}
        self.assertEqual(with_forms, {"inbox", "outbox"})
        for s in self.pack["schemes"]:
            for form in s.get("forms") or []:
                rx = re.compile(form["pattern"])
                self.assertTrue(rx.fullmatch(form["example"]), form)
                for bad in ("not-an-address", "a@b c", "a b@c.d", "@example.org"):
                    self.assertIsNone(rx.fullmatch(bad), bad)

    def test_the_provider_has_no_transport(self) -> None:
        self.assertEqual(self.pack["providers"], [{"id": "imap", "label": "IMAP/SMTP"}])

    def test_unmapped_is_exactly_the_unclassified_verbs(self) -> None:
        mapped = {v["verb"] for v in self.pack["verbs"]} | set(self.pack["taint"])
        self.assertEqual(sorted(self.pack["unmapped"]),
                         sorted(set(EMAIL_VERBS) - mapped))
        self.assertEqual(sorted(self.pack["unmapped"]),
                         ["email.folders", "email.save_attachment"])


class ComponentsBlockTests(unittest.TestCase):
    def test_one_gateway_component_from_this_repository(self) -> None:
        doc = _pack_doc()
        self.assertEqual(doc["version"], "2.1.0")
        self.assertIs(doc["first_party"], True)
        # 2.1.0 adds Telegram's component (#522, test_522_telegram_component).
        (c,) = [x for x in doc["components"] if x["id"] == "mail"]
        self.assertEqual(c, {
            "id": "mail", "target": "gateway",
            "source": {"repo": "r-clodia/clodia-logic",
                       "path": "catalogs/packs/comms-pack/gateway", "ref": "main"},
            "entry": "mail_component", "connectors": ["mail"],
            "credentials": ["mailbox_"]})

    def test_the_source_path_and_entry_exist(self) -> None:
        self.assertTrue((GATEWAY / "mail_component.py").is_file())
        self.assertEqual(list(GATEWAY.rglob("__pycache__")), [])
        self.assertEqual([p for p in GATEWAY.rglob("*") if p.suffix in (".pyc", ".so")], [])


class PluginTests(unittest.TestCase):
    def test_no_transport_left(self) -> None:
        # 2.1.0: no MCP backend at all — Telegram is a gateway component too.
        self.assertFalse((PLUGIN / ".mcp.json").exists())
        for gone in ("mail_transport_mcp.py", "mail_transport_core.py", "email_client.py"):
            self.assertFalse((PLUGIN / "mcp" / gone).exists(), gone)


# ══════════════════════════════════════════════════════════════════════════
# 2. the component on a fake host
# ══════════════════════════════════════════════════════════════════════════

PKG = "cpm558"


def _load_component():
    """Import `gateway/` as a package of its own, without writing bytecode."""
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
        return importlib.import_module(f"{PKG}.mail_component")
    finally:
        sys.dont_write_bytecode = before


def _mod(name: str):
    return sys.modules[f"{PKG}.{name}"]


class FakeCredentials:
    def __init__(self, host, prefix):
        self.host, self.prefix = host, prefix

    def _check(self, name):
        if not (name.startswith(self.prefix) and len(name) > len(self.prefix)):
            raise PermissionError(f"{name} outside {self.prefix}")

    def list(self):
        return [n for n in self.host.vault if n.startswith(self.prefix)]

    def has(self, name):
        self._check(name)
        return name in self.host.vault

    def read_internal(self, name):
        self._check(name)
        if name not in self.host.vault:
            raise PermissionError(f"absent: {name}")
        return dict(self.host.vault[name])

    def deposit(self, name, bundle, *, cred_type="opaque", grant_agents=()):
        self._check(name)
        self.host.vault[name] = dict(bundle)
        self.host.deposits.append((name, cred_type, list(grant_agents)))

    def remove(self, name):
        self._check(name)
        return self.host.vault.pop(name, None) is not None


class FakePerimeter:
    def __init__(self):
        self.inbox: set[str] = set()
        self.outbox: set[str] = set()
        self.vetted: set[str] = set()
        self.strict = None
        self.legacy: dict = {}

    def mailbox_allowed(self, direction, address, scope=None):
        return address in (self.inbox if direction == "inbox" else self.outbox)

    def strict_scope(self):
        return self.strict

    def address_of(self, header):
        import re
        m = re.search(r"[\w.+-]+@[\w.-]+", header or "")
        return m.group(0).lower() if m else ""

    def source_vetted(self, uri, scope=None):
        return uri in self.vetted

    def legacy_mail_accounts(self):
        return dict(self.legacy)

    #: Accounts that ALSO hold a Google mail credential (names only).
    google_named: set = set()

    def google_mail_account_exists(self, account):
        return account in self.google_named


class FakeTopics:
    def __init__(self, host):
        self.host = host
        self.files: dict = {}

    def read_file(self, tier, name, path, *, crossing="file"):
        if (tier, name) not in self.host.members:
            raise PermissionError(f"not a member of {tier}/{name}")
        data = self.files[(tier, name, path)]
        if crossing:
            self.host.crossings.append(("data", crossing))
        return data

    def put_file(self, tier, name, path, data, *, provenance="untrusted"):
        if (tier, name) not in self.host.members:
            raise PermissionError(f"not a member of {tier}/{name}")
        self.files[(tier, name, f"local/{path}")] = bytes(data)
        if provenance == "untrusted":
            self.host.tainted.append((f"{tier}/{name}", path))
        return {"path": f"local/{path}", "provenance": provenance}


class FakeHost:
    """What `server.components.host.Host` offers, recorded."""

    class AuditUnavailable(RuntimeError):
        pass

    def __init__(self, scratch: str):
        import logging
        self.verbs, self.routes, self.services = {}, {}, {}
        self.vault: dict[str, dict] = {}
        self.deposits: list = []
        self.crossings: list = []
        self.decisions: list = []
        self.tainted: list = []
        self.members: set = set()
        self.topic = (None, None)
        self.perimeter = FakePerimeter()
        self.topics = FakeTopics(self)
        self.scratch = scratch
        self.google: dict = {}
        self.service_lookups: list = []
        self.allowed = True
        self.log = logging.getLogger("fake-host")

    def register_verb(self, tool, handler, *, offload=True):
        assert tool.name not in self.verbs
        self.verbs[tool.name] = (tool, handler)

    def register_route(self, path, methods, handler, *, auth="owner"):
        self.routes[path] = (methods, handler, auth)

    def register_service(self, name, handler):
        assert name not in self.services
        self.services[name] = handler

    def credentials(self, prefix):
        if prefix != "mailbox_":
            raise PermissionError(f"'{prefix}' is not declared")
        return FakeCredentials(self, prefix)

    def service(self, pack, name):
        self.service_lookups.append((pack, name))
        if (pack, name) not in {("google-pack", "mail_accounts"),
                                ("google-pack", "mail_exec")}:
            raise PermissionError("not granted")
        return self.google.get(name)

    def scratch_path(self, path):
        full = os.path.realpath(path)
        if not full.startswith(os.path.realpath(self.scratch) + os.sep):
            raise ValueError("outside the scratch")
        return full

    def note_crossing(self, data=None, *, path=None, kind="file"):
        self.crossings.append(("path" if path else "data", kind))

    def agent(self):
        return "messaggero"

    def current_topic(self):
        return self.topic

    def tool_allowed(self, verb):
        if not self.allowed:
            raise PermissionError(f"tool '{verb}' not in allowed_tools")

    def audit_decision(self, verb, kind, result, *, reason_class=None):
        self.decisions.append((verb, kind, result, reason_class))


#: A stand-in for email_client.py: reports what it sees.
_PROBE = textwrap.dedent(r"""
    import json, os, stat, sys
    d = os.environ.get("CLODIA_SECRETS_DIR") or ""
    cfg = json.load(open(os.path.join(d, "email_config.json"))) if d else None
    print(json.dumps({"argv": sys.argv[1:], "config": cfg, "env": sorted(os.environ),
                      "dir_mode": oct(stat.S_IMODE(os.stat(d).st_mode)) if d else None,
                      "secrets_dir": d}))
""")

STUDIO = {"email": "Studio@Example.it", "password": SECRET,
          "imap_server": "imap.example.it", "smtp_server": "smtp.example.it",
          "smtp_port": 587}


class _OnFakeHost(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host = FakeHost(self.tmp)
        self.comp = _load_component()
        before = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            self.comp.register(self.host)
        finally:
            sys.dont_write_bytecode = before
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == PKG or k.startswith(PKG + ".")])
        self.host.vault["mailbox_studio"] = dict(STUDIO)
        self.host.perimeter.inbox.add("studio@example.it")
        self.host.perimeter.outbox.add("studio@example.it")
        self.mail = _mod("mail")
        probe = Path(self.tmp) / "probe.py"
        probe.write_text(_PROBE, encoding="utf-8")
        p = patch.object(self.mail, "_EMAIL_SCRIPT", str(probe))
        p.start()
        self.addCleanup(p.stop)

    def call(self, verb, args):
        return self.host.verbs[verb][1](args)


class RegisterTests(_OnFakeHost):
    def test_the_8_verbs_and_services(self) -> None:
        self.assertEqual(sorted(self.host.verbs), EMAIL_VERBS)
        self.assertEqual(self.host.routes, {})
        self.assertEqual(sorted(self.host.services), SERVICES)

    def test_no_import_of_the_gateway(self) -> None:
        for path in GATEWAY.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    mods = [node.module or ""]
                for m in mods:
                    self.assertFalse(m == "server" or m.startswith("server."),
                                     f"{path.name} imports {m}")

    def test_the_refusal_phrase(self) -> None:
        self.assertEqual(self.mail.MITTENTE_NON_VAGLIATO, "mittente non vagliato")


class ImapTests(_OnFakeHost):
    def test_the_client_runs_on_this_calls_mailbox_only(self) -> None:
        self.host.vault["mailbox_altra"] = {"email": "a@x.it", "password": "other",
                                            "smtp_server": "s", "smtp_port": 1}
        out = self.call("email.folders", {"account": "studio"})
        seen = out["folders"]
        self.assertEqual(seen["argv"], ["--account", "studio", "folders"])
        self.assertEqual(seen["config"], {"default": "studio",
                                          "accounts": {"studio": STUDIO}})
        self.assertEqual(seen["dir_mode"], "0o700")
        # Minimal environment; Python itself may add LC_CTYPE, macOS its CF encoding.
        extra = set(seen["env"]) - {"PATH", "LANG", "HOME", "CLODIA_SECRETS_DIR",
                                    "PYTHONDONTWRITEBYTECODE"}
        self.assertLessEqual(extra, {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}, extra)
        self.assertFalse(Path(seen["secrets_dir"]).exists())
        self.assertEqual(out["available_accounts"], ["altra", "studio"])

    def test_the_mailbox_whitelist_comes_first(self) -> None:
        self.host.perimeter.inbox.clear()
        with patch.object(self.mail.subprocess, "run") as run:
            with self.assertRaises(PermissionError) as e:
                self.call("email.list", {"account": "studio"})
        run.assert_not_called()
        self.assertIn("inbox:studio@example.it", str(e.exception))

    def test_a_send_only_mailbox_is_not_read(self) -> None:
        self.host.vault["mailbox_studio"].pop("imap_server")
        with self.assertRaises(PermissionError) as e:
            self.call("email.list", {"account": "studio"})
        self.assertIn("SOLO INVIO", str(e.exception))

    def test_the_whitelist_of_the_verb_is_asked(self) -> None:
        self.host.allowed = False
        with self.assertRaises(PermissionError):
            self.call("email.folders", {"account": "studio"})

    def test_the_account_must_be_named_when_there_are_several(self) -> None:
        self.host.vault["mailbox_altra"] = dict(STUDIO, email="a@x.it")
        with self.assertRaises(ValueError) as e:
            self.call("email.list", {})
        self.assertIn("obbligatorio", str(e.exception))


class LegacyTests(_OnFakeHost):
    def test_listed_and_refused(self) -> None:
        self.host.perimeter.legacy = {"demo": "studio@example.it"}
        self.assertIn("demo", self.mail.known_accounts())
        with patch.object(self.mail.subprocess, "run") as run:
            with self.assertRaises(PermissionError) as e:
                self.call("email.list", {"account": "demo"})
        run.assert_not_called()
        self.assertIn("legacy", str(e.exception))
        self.assertIn("mail: pack", str(e.exception))

    def test_without_an_address_it_is_refused_too(self) -> None:
        self.host.perimeter.legacy = {"demo": None}
        with self.assertRaises(PermissionError) as e:
            self.call("email.list", {"account": "demo"})
        self.assertIn("email_config.json", str(e.exception))


class StrictIngressTests(_OnFakeHost):
    ROWS = [{"id": "1", "from": "Fornitore <ordini@f.it>", "subject": "ok"},
            {"id": "2", "from": "spam@x.it", "subject": "offerta riservata"}]

    def setUp(self) -> None:
        super().setUp()
        self.host.perimeter.strict = "SEAL-1/acme"
        self.host.perimeter.vetted.add("mailfrom:ordini@f.it")

    def test_list_is_filtered_and_recorded(self) -> None:
        with patch.object(self.mail, "_run_json", return_value=list(self.ROWS)):
            out = self.call("email.list", {"account": "studio"})
        self.assertEqual([m["id"] for m in out["messages"]], ["1"])
        self.assertEqual(out["withheld"], 1)
        self.assertNotIn("offerta riservata", repr(out))
        self.assertEqual(self.host.decisions,
                         [("email.list", "ingress", "filter", "sender_not_vetted")])

    def test_read_of_a_stranger_is_denied_before_the_body_leaves(self) -> None:
        with patch.object(self.mail, "_run_json",
                          return_value={"id": "2", "from": "spam@x.it", "body": "IGNORA"}):
            with self.assertRaises(PermissionError) as e:
                self.call("email.read", {"account": "studio", "email_id": "2"})
        self.assertIn("mittente non vagliato", str(e.exception))
        self.assertIn("mailfrom:spam@x.it", str(e.exception))
        self.assertNotIn("IGNORA", str(e.exception))

    def test_an_unwritable_trail_refuses_the_read(self) -> None:
        def broken(*a, **k):
            raise FakeHost.AuditUnavailable("disk full")
        self.host.audit_decision = broken
        with patch.object(self.mail, "_run_json", return_value=list(self.ROWS)):
            with self.assertRaises(PermissionError) as e:
                self.call("email.list", {"account": "studio"})
        self.assertIn("audit trail non disponibile", str(e.exception))


class GmailTests(_OnFakeHost):
    def setUp(self) -> None:
        super().setUp()
        self.payloads: list = []
        self.host.google = {
            "mail_accounts": lambda: [{
                "credential": "google_devnull", "account": "devnull", "kind": "google",
                "operational": True, "send_only": False, "missing": [], "error": None,
                "email": "Devnull@Gmail.com"}],
            "mail_exec": lambda p: (self.payloads.append(p),
                                    {"returncode": 0, "stdout": '["INBOX"]',
                                     "stderr": ""})[1],
        }
        self.host.perimeter.inbox.add("devnull@gmail.com")
        self.host.perimeter.outbox.add("devnull@gmail.com")

    def test_a_google_account_goes_to_google_pack_without_a_secret(self) -> None:
        att = Path(self.tmp) / "offerta.pdf"
        att.write_bytes(b"%PDF")
        with patch.object(self.mail.subprocess, "run") as run:
            out = self.call("email.folders", {"account": "devnull"})
            self.call("email.send", {"account": "devnull", "to": "x@y.it",
                                     "subject": "s", "body": "b",
                                     "attachments": [str(att)]})
        run.assert_not_called()
        self.assertEqual(out["folders"], ["INBOX"])
        folders, send = self.payloads
        self.assertEqual(folders, {"account": "devnull", "argv": ["folders"],
                                   "files": {}, "timeout": 60})
        self.assertEqual(sorted(send["files"]), ["att/0/offerta.pdf"])
        self.assertIn("{dir}/att/0/offerta.pdf", send["argv"])
        self.assertNotIn(str(att), json.dumps(send))
        self.assertEqual(self.mail.system_mailboxes()[0],
                         {"account": "devnull", "email": "devnull@gmail.com",
                          "send_only": False})

    def test_the_whitelist_is_checked_on_the_google_address(self) -> None:
        self.host.perimeter.inbox.clear()
        with self.assertRaises(PermissionError) as e:
            self.call("email.list", {"account": "devnull"})
        self.assertIn("inbox:devnull@gmail.com", str(e.exception))
        self.assertEqual(self.payloads, [])

    def test_without_google_pack_the_account_is_refused_and_says_why(self) -> None:
        self.host.google = {}
        with self.assertRaises(ValueError) as e:
            self.call("email.folders", {"account": "devnull"})
        self.assertIn("google-pack", str(e.exception))
        out = self.call("email.folders", {"account": "studio"})     # IMAP unaffected
        self.assertEqual(out["folders"]["argv"][2], "folders")

    def test_a_dual_credential_account_is_refused_while_google_pack_is_down(self) -> None:
        """Review of #384: `studio` with both `mailbox_studio` and
        `google_studio` silently switched sender address when google-pack was
        down. Refused instead."""
        self.host.google = {}
        self.host.perimeter.google_named = {"studio"}
        with self.assertRaises(PermissionError) as e:
            self.call("email.folders", {"account": "studio"})
        self.assertIn("google-pack", str(e.exception))

    def test_the_google_bundle_is_never_read(self) -> None:
        with self.assertRaises(PermissionError):
            self.host.credentials("google_")
        self.call("email.folders", {"account": "devnull"})
        self.assertEqual(set(self.host.service_lookups),
                         {("google-pack", "mail_accounts"), ("google-pack", "mail_exec")})


class ReplyRecipientTests(_OnFakeHost):
    def test_the_recipient_is_the_sender_of_the_original(self) -> None:
        svc = self.host.services["pdp_arguments"]
        with patch.object(self.mail, "_run_json",
                          return_value={"id": "7", "from": "Mario <Mario@X.it>"}):
            self.assertEqual(svc("email.reply", {"account": "studio", "email_id": "7"}),
                             {"to": "mario@x.it"})
        self.assertEqual(svc("email.send", {"to": "x@y.it"}), {})

    def test_unresolvable_is_empty_never_the_callers(self) -> None:
        svc = self.host.services["pdp_arguments"]
        with patch.object(self.mail, "_run_json", side_effect=RuntimeError("imap")):
            self.assertEqual(svc("email.reply", {"account": "studio", "email_id": "7",
                                                 "to": "friend@ok.it"}), {"to": ""})
        self.host.allowed = False                 # the read is `email.read`'s
        self.assertEqual(svc("email.reply", {"account": "studio", "email_id": "7"}),
                         {"to": ""})


class TopicAttachmentTests(_OnFakeHost):
    def setUp(self) -> None:
        super().setUp()
        self.host.members.add(("SEAL-1", "acme"))
        self.host.topic = ("SEAL-1", "acme")

    def test_save_attachment_lands_untrusted_and_taints(self) -> None:
        with patch.object(self.mail, "_run_json", return_value={
                "filename": "x.pdf", "content_type": "application/pdf",
                "data": base64.b64encode(b"%PDF ostile").decode()}):
            r = self.call("email.save_attachment", {"account": "studio", "email_id": "7",
                                                    "filename": "Tomato / NDA.pdf"})
        self.assertEqual(r["topic_path"], "local/Tomato - NDA.pdf")
        self.assertEqual((r["provenance"], r["tainted"]), ("untrusted", True))
        self.assertEqual(self.host.tainted, [("SEAL-1/acme", "Tomato - NDA.pdf")])

    def test_save_attachment_into_the_scratch(self) -> None:
        dest = os.path.join(self.tmp, "spawn", "a.pdf")
        with patch.object(self.mail, "_run_json", return_value={
                "filename": "a.pdf", "data": base64.b64encode(b"x").decode()}):
            r = self.call("email.save_attachment", {"account": "studio", "email_id": "7",
                                                    "filename": "a.pdf", "dest": dest})
        self.assertEqual(Path(r["local_path"]).read_bytes(), b"x")
        with self.assertRaises(ValueError):
            self.call("email.save_attachment", {"account": "studio", "email_id": "7",
                                                "filename": "a.pdf", "dest": "/etc/x"})

    def test_topic_files_and_scratch_files_leave_as_attachments(self) -> None:
        self.host.topics.files[("SEAL-1", "acme", "files/c.pdf")] = b"%PDF c"
        own = Path(self.tmp) / "mio.pdf"
        own.write_bytes(b"%PDF mio")
        seen = {}

        def send(to, subject, body, *, account, cc, attachments):
            seen["files"] = {Path(p).name: Path(p).read_bytes() for p in attachments}
            return {"ok": True}
        with patch.object(self.mail, "send", send):
            self.call("email.send", {"account": "studio", "to": "x@y.it", "subject": "s",
                                     "body": "b", "attachments": [str(own)],
                                     "topic_files": ["files/c.pdf"]})
        self.assertEqual(seen["files"], {"mio.pdf": b"%PDF mio", "c.pdf": b"%PDF c"})
        self.assertEqual(self.host.crossings, [("path", "attachment"),
                                               ("data", "attachment")])

    def test_an_attachment_outside_the_scratch_is_refused(self) -> None:
        with self.assertRaises(ValueError) as e:
            self.call("email.send", {"account": "studio", "to": "x@y.it", "subject": "s",
                                     "body": "b", "attachments": ["/etc/passwd"]})
        self.assertIn("outside your scratch", str(e.exception))

    def test_a_non_member_topic_is_refused(self) -> None:
        self.host.topics.files[("SEAL-2", "altro", "files/c.pdf")] = b"x"
        with self.assertRaises(PermissionError):
            self.call("email.send", {"account": "studio", "to": "x@y.it", "subject": "s",
                                     "body": "b", "topic_files": ["files/c.pdf"],
                                     "tier": "SEAL-2", "name": "altro"})


class MailboxServicesTests(_OnFakeHost):
    def test_add_list_remove(self) -> None:
        add = self.host.services["mailbox_add"]
        self.assertEqual(add({"account": "Info", "email": "info@x.it", "password": "p",
                              "smtp_server": "smtp.x.it"}),
                         {"status": 200, "body": {"account": "info", "connected": True}})
        self.assertEqual(self.host.deposits[-1],
                         ("mailbox_info", "mailbox", ["clodia", "ophelia"]))
        self.assertEqual(add({"account": "x"})["status"], 400)
        self.assertEqual(add({"account": "../x", "email": "e", "password": "p",
                              "smtp_server": "s"})["status"], 400)
        self.assertEqual(self.host.services["mailboxes_list"]()["mailboxes"],
                         ["info", "studio"])
        self.assertEqual(self.host.services["mailbox_remove"]("info"),
                         {"removed": True, "email": "info@x.it"})
        self.assertNotIn("mailbox_info", self.host.vault)

    def test_test_mailboxes_reports_without_the_secret(self) -> None:
        import imaplib

        class _Imap:
            def __init__(self, *a, **k):
                pass

            def login(self, user, pwd):
                raise imaplib.IMAP4.error(f"bad {pwd}")

            def logout(self):
                pass
        with patch.object(imaplib, "IMAP4_SSL", _Imap):
            out = self.host.services["test_mailboxes"]()
        self.assertFalse(out["ok"])
        self.assertNotIn(SECRET, json.dumps(out))


# ══════════════════════════════════════════════════════════════════════════
# 3. the bundled client
# ══════════════════════════════════════════════════════════════════════════

def _sans_note(text: str) -> str:
    start = text.index("\nVendored into")
    end = text.index('"""', start)
    return text[:start] + text[end:]


class ClientTests(unittest.TestCase):
    def test_the_same_client_as_google_packs(self) -> None:
        self.assertEqual(_sans_note(CLIENT.read_text(encoding="utf-8")),
                         _sans_note(GOOGLE_CLIENT.read_text(encoding="utf-8")))
        self.assertIn("r-clodia/clodia-tools:vendor/email_client.py",
                      CLIENT.read_text(encoding="utf-8")[:1200])

    @unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
    def test_the_gateways_copy(self) -> None:
        self.assertEqual(_sans_note(CLIENT.read_text(encoding="utf-8")),
                         (Path(TOOLS_SRC) / "vendor" / "email_client.py")
                         .read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════
# 4. against the gateway's sources (CLODIA_TOOLS_SRC)
# ══════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(TOOLS_SRC, "set CLODIA_TOOLS_SRC to a clodia-tools checkout")
class GatewaySourcesTests(unittest.TestCase):
    def test_the_schemas_are_the_gateways(self) -> None:
        from mcp.types import Tool
        src = (Path(TOOLS_SRC) / "server" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        ns: dict = {"Tool": Tool}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == \
                    "_EMAIL_TOOLS":
                exec(f"_EMAIL_TOOLS = {ast.get_source_segment(src, node.value)}", ns)
        native = ns["_EMAIL_TOOLS"]
        _load_component()
        before = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            mine = importlib.import_module(f"{PKG}.schemas").TOOLS
        finally:
            sys.dont_write_bytecode = before

        def shape(t):
            return (t.name, t.description,
                    getattr(t, "inputSchema", None) or getattr(t, "input_schema", None))
        self.assertEqual([shape(t) for t in mine], [shape(t) for t in native])

    def test_the_refusal_phrase_is_the_gateways(self) -> None:
        src = (Path(TOOLS_SRC) / "server" / "tools" / "email.py").read_text(encoding="utf-8")
        self.assertIn('MITTENTE_NON_VAGLIATO = "mittente non vagliato"', src)

    def test_the_gateways_import_guard_finds_nothing(self) -> None:
        root = Path(TOOLS_SRC)
        sys.path.insert(0, str(root))
        try:
            guard = _load(root / "server" / "components" / "guard.py", "guard_558m")
        finally:
            sys.path.remove(str(root))
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "gateway"
            shutil.copytree(GATEWAY, dst, ignore=shutil.ignore_patterns("__pycache__"))
            self.assertEqual(guard.scan_tree(dst), [])

    def test_the_grant_table_names_these_services(self) -> None:
        src = (Path(TOOLS_SRC) / "server" / "connectors" / "builtin.py").read_text(
            encoding="utf-8")
        self.assertIn('("google-pack", "mail_accounts")', src)
        self.assertIn('("google-pack", "mail_exec")', src)
        self.assertIn('"mailbox_": "mail"', src)


class StrictIngressNonListTests(_OnFakeHost):
    """clodia-platform#560: a list/search result that is not a list (e.g.
    `{"raw": …}` when the client's JSON did not parse) reveals nothing on a
    strict channel."""

    def test_a_non_list_result_reveals_nothing(self):
        with patch.object(self.mail, "_record_ingress") as rec:
            kept, hidden = self.mail._filter_by_sender({"raw": "From: x@evil"}, "s", "email.list")
        self.assertEqual((kept, hidden), ([], 1))
        rec.assert_called_once()
        self.assertEqual(self.mail._filter_by_sender(None, "s", "email.list"), ([], 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
