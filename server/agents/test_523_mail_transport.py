"""clodia-platform#523 — comms-pack declares `mail` and ships its transport.

The gateway keeps the `email.*` verbs and every decision; the pack (a) declares
the `mail` connector — the gateway's builtin manifest, moved — and (b) ships the
IMAP/SMTP transport: a backend `mail-transport` with one tool `run`, which the
gateway calls with only that call's credential files and attachments.

What could break silently, and is measured here:

1. **the declaration drifts from the builtin.** On migration day the gateway's
   `mail.yaml` leaves service and this one takes its place: any difference
   other than the two declared additions (`forms:` on inbox/outbox and
   `providers:`) is the gate changing what the owner consented to. A copy of
   the builtin is vendored under `fixtures/` because the suite runs offline;
2. **the contract with the gateway drifts.** The gateway side is written
   against EXACTLY: backend `mail-transport`, tool `run`, arguments `argv`,
   `files`, `timeout`, result `{returncode, stdout, stderr}`;
3. **the transport leaks.** Files only inside a private dir, never through
   `..`, an absolute path or a link; the dir always removed — on failure and on
   timeout too; no credential echoed back in stderr.

The transport is imported from the catalog path, like `telegram_core`: it lives
inside the pack and is stdlib-only, so no `mcp` is needed here.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ..config import workspace_path

PACK = Path(workspace_path("catalogs/packs/comms-pack"))
PLUGIN = PACK / "plugins" / "comms-pack"
MCP_DIR = PLUGIN / "mcp"
CORE_FILE = MCP_DIR / "mail_transport_core.py"
ENTRYPOINT = MCP_DIR / "mail_transport_mcp.py"
CLIENT = MCP_DIR / "email_client.py"
MCP_JSON = PLUGIN / ".mcp.json"
BUILTIN = Path(__file__).resolve().parent / "fixtures" / "523_builtin_mail.yaml"

SECRET = "S3cr3t-Pa55w0rd-523-xyz"  # gitleaks:allow — fake test credential


def _core():
    spec = importlib.util.spec_from_file_location("mail_transport_core_pack", CORE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connector(doc: dict, cid: str = "mail") -> dict:
    for entry in doc.get("connectors") or []:
        if isinstance(entry, dict) and entry.get("id") == cid:
            return entry
    raise AssertionError(f"no connector '{cid}'")


def _pack_mail() -> dict:
    return _connector(yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8")))


def _b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode()


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
        sec = os.path.join(os.environ["CLODIA_SECRETS_DIR"], "email_config.json")
        sys.stderr.write("config was: " + open(sec).read())
        sys.exit(1)
""")


class _IsolatedTmp(unittest.TestCase):
    """Every test gets its own TMPDIR, so "the dir was removed" is checkable."""

    def setUp(self) -> None:
        self.core = _core()
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


class DeclarationMatchesTheBuiltinTests(unittest.TestCase):
    """The pack's `mail` is the gateway's builtin plus exactly two additions."""

    def setUp(self) -> None:
        self.pack = _pack_mail()
        self.builtin = _connector(yaml.safe_load(BUILTIN.read_text(encoding="utf-8")))

    def test_same_connector_except_the_two_additions(self) -> None:
        pack = json.loads(json.dumps(self.pack))
        pack.pop("providers")
        for scheme in pack["schemes"]:
            if scheme["scheme"] in ("inbox", "outbox"):
                scheme.pop("forms")
        self.assertEqual(pack, self.builtin)

    def test_every_field_compared_is_really_there(self) -> None:
        # A comparison of two empty blocks would pass too.
        for key in ("schemes", "verbs", "topic_actions", "tools_card", "taint"):
            self.assertTrue(self.builtin.get(key), key)
        self.assertEqual(self.pack["namespace"], "email")
        self.assertEqual(self.pack["manifest_version"], self.builtin["manifest_version"])
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
                self.assertTrue(rx.fullmatch("studio@davidecarboni.it"))
                for bad in ("not-an-address", "a@b c", "a b@c.d", "@example.org"):
                    self.assertIsNone(rx.fullmatch(bad), bad)

    def test_the_provider_is_the_transport_the_gateway_calls(self) -> None:
        self.assertEqual(self.pack["providers"], [{
            "id": "imap", "label": "IMAP/SMTP",
            "transport": {"backend": "mail-transport", "tool": "run"}}])


class MountTests(unittest.TestCase):
    """`.mcp.json` mounts the transport the provider names, with no secrets."""

    def setUp(self) -> None:
        self.mount = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]
        self.core = _core()

    def test_backend_named_as_the_provider_says(self) -> None:
        backend = _pack_mail()["providers"][0]["transport"]["backend"]
        self.assertEqual(backend, "mail-transport")
        self.assertIn(backend, self.mount)
        args = [str(a) for a in self.mount[backend].get("args") or []]
        self.assertEqual(len(args), 1, args)
        self.assertIn("/plugins/comms-pack/mcp/", args[0])
        self.assertTrue(args[0].endswith(f"/{ENTRYPOINT.name}"), args[0])
        self.assertTrue(ENTRYPOINT.is_file())

    def test_no_vault_placeholder(self) -> None:
        # Credentials arrive per call, as files: a mount-time secret would give
        # the process a mailbox for its whole life, whichever call it serves.
        entry = json.dumps(self.mount["mail-transport"])
        self.assertNotIn("VAULT", entry)
        self.assertNotIn("env", self.mount["mail-transport"])

    def test_exactly_the_tool_and_arguments_the_gateway_expects(self) -> None:
        self.assertEqual([t["name"] for t in self.core.TOOL_SPECS], ["run"])
        schema = self.core.TOOL_SPECS[0]["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"argv", "files", "timeout"})
        self.assertEqual(schema["required"], ["argv"])
        self.assertIn("GATEWAY-INTERNAL", self.core.TOOL_SPECS[0]["description"])
        self.assertEqual(set(self.core.DISPATCH), {"run"})

    def test_the_envelope_does_not_redeclare_the_surface(self) -> None:
        src = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("core.TOOL_SPECS", src)
        self.assertIn("core.DISPATCH", src)

    def test_the_bundled_client_is_the_gateway_copy(self) -> None:
        self.assertEqual(self.core.CLIENT, CLIENT.resolve())
        head = CLIENT.read_text(encoding="utf-8")[:1200]
        self.assertIn("r-clodia/clodia-tools:vendor/email_client.py", head)
        self.assertIn("CLODIA_SECRETS_DIR", CLIENT.read_text(encoding="utf-8"))


class RefusalTests(_IsolatedTmp):
    """Bad paths are refused before anything is created."""

    def _refused(self, rel: str) -> None:
        with self.with_probe():
            with self.assertRaises(self.core.TransportError, msg=rel):
                self.core.run({"argv": [], "files": {rel: _b64("x")}})
        self.assertEqual(self.leftovers(), [], rel)

    def test_absolute_paths(self) -> None:
        for rel in ("/etc/passwd", "/tmp/x", "C:/x", "c:x"):
            self._refused(rel)

    def test_traversal_and_odd_paths(self) -> None:
        for rel in ("..", "../x", "a/../../x", "secrets/../../x", "./x",
                    "a//b", "a\\..\\x", "", "a\x00b"):
            self._refused(rel)

    def test_bad_types(self) -> None:
        with self.with_probe():
            for bad in ({"argv": "accounts"}, {"argv": [1]}, {"argv": [], "files": []},
                        {"argv": [], "files": {"a": "not base64!!"}},
                        {"argv": [], "timeout": "60"}, {"argv": [], "timeout": 0}):
                with self.assertRaises(self.core.TransportError, msg=bad):
                    self.core.run(bad)
        self.assertEqual(self.leftovers(), [])

    def test_conflicting_paths_clean_up_what_was_written(self) -> None:
        with self.with_probe():
            with self.assertRaises(self.core.TransportError):
                self.core.run({"argv": [], "files": {"a": _b64("1"), "a/b": _b64("2")}})
        self.assertEqual(self.leftovers(), [])

    def test_timeout_is_clamped(self) -> None:
        self.assertEqual(self.core._check_request({"argv": []})[2], 60)
        self.assertEqual(self.core._check_request({"argv": [], "timeout": 10_000})[2], 300)


class RunTests(_IsolatedTmp):

    def test_files_land_inside_the_private_dir_with_tight_modes(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["ok"], "files": {
                "secrets/email_config.json": _b64("{}"),
                "att/report.pdf": _b64(b"%PDF-1.4")}})
        self.assertEqual(res["returncode"], 0, res)
        seen = json.loads(res["stdout"])
        self.assertEqual(seen["root_mode"], "0o700")
        self.assertEqual(seen["files"]["secrets/email_config.json"], "0o600")
        self.assertEqual(seen["files"]["att/report.pdf"], "0o600")
        self.assertEqual(seen["files"]["secrets/"], "0o700")
        self.assertTrue(Path(seen["cwd"]).resolve().is_relative_to(self.tmp.resolve()))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(set(res), {"returncode", "stdout", "stderr"})

    def test_dir_placeholder_and_minimal_env(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["ok", "--attachment", "{dir}/att/a.txt",
                                          "plain"],
                                 "files": {"att/a.txt": _b64("hi")}})
        seen = json.loads(res["stdout"])
        root = seen["cwd"]
        self.assertEqual(seen["argv"][2], os.path.join(root, "att/a.txt"))
        self.assertNotIn("{dir}", " ".join(seen["argv"]))
        env = seen["env"]
        self.assertEqual(env["CLODIA_SECRETS_DIR"], os.path.join(root, "secrets"))
        self.assertEqual(os.path.realpath(env["HOME"]), os.path.realpath(root))
        # Python itself may add LC_CTYPE (PEP 538) and macOS its CF encoding.
        extra = set(env) - {"PATH", "LANG", "HOME", "CLODIA_SECRETS_DIR"}
        self.assertLessEqual(extra, {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}, extra)

    def test_dir_is_never_substituted_in_agent_text(self) -> None:
        """Review of #528: subject/body/cc containing `{dir}` reached the
        recipient rewritten with this directory. Only the attachment slot."""
        with self.with_probe():
            res = self.core.run({"argv": ["ok", "--subject", "Offerta {dir}",
                                          "--body", "vedi {dir}/x ok",
                                          "--cc", "{dir}/a@b.it",
                                          "--attachment", "{dir}/att/a.txt"],
                                 "files": {"att/a.txt": _b64("hi")}})
        seen = json.loads(res["stdout"])
        self.assertEqual(seen["argv"][2], "Offerta {dir}")
        self.assertEqual(seen["argv"][4], "vedi {dir}/x ok")
        self.assertEqual(seen["argv"][6], "{dir}/a@b.it")
        self.assertEqual(seen["argv"][8], os.path.join(seen["cwd"], "att/a.txt"))

    def test_dir_removed_on_failure(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["fail"], "files": {"x": _b64("1")}})
        self.assertEqual(res["returncode"], 3)
        self.assertEqual(self.leftovers(), [])

    def test_dir_removed_on_timeout(self) -> None:
        with self.with_probe():
            res = self.core.run({"argv": ["sleep"], "files": {"x": _b64("1")},
                                 "timeout": 1})
        self.assertEqual(res["returncode"], self.core.TIMEOUT_RETURNCODE)
        self.assertIn("timed out", res["stderr"])
        self.assertEqual(self.leftovers(), [])

    def test_dir_removed_when_the_launch_itself_fails(self) -> None:
        with patch.object(self.core, "CLIENT", self.box / "probe.py"), \
                patch.object(self.core.subprocess, "Popen", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.core.run({"argv": [], "files": {"x": _b64("1")}})
        self.assertEqual(self.leftovers(), [])

    def test_credentials_are_scrubbed_from_stderr(self) -> None:
        cfg = json.dumps({"accounts": {"a": {"email": "a@example.org",
                                             "password": SECRET}}})
        with self.with_probe():
            res = self.core.run({"argv": ["echo-secrets"],
                                 "files": {"secrets/email_config.json": _b64(cfg)}})
        self.assertNotIn(SECRET, res["stderr"])
        self.assertIn(self.core.REDACTED, res["stderr"])

    def test_stderr_is_trimmed(self) -> None:
        self.assertLessEqual(len(self.core._trim("x" * 100_000)),
                             self.core.STDERR_LIMIT + 32)


class RealClientRoundTripTests(_IsolatedTmp):
    """The bundled email_client.py really runs, reading CLODIA_SECRETS_DIR."""

    def _cfg(self, **acc) -> str:
        acc = {"email": "studio@example.org", "password": SECRET, **acc}
        return _b64(json.dumps({"accounts": {"studio": acc}, "default": "studio"}))

    def test_reads_the_config_it_was_handed(self) -> None:
        res = self.core.run({"argv": ["accounts"],
                             "files": {"secrets/email_config.json": self._cfg()}})
        self.assertEqual(res["returncode"], 0, res["stderr"])
        accounts = json.loads(res["stdout"])
        self.assertEqual(accounts[0]["email"], "studio@example.org")
        self.assertNotIn(SECRET, res["stdout"] + res["stderr"])
        self.assertEqual(self.leftovers(), [])

    def test_no_config_means_no_account(self) -> None:
        # Without files the client sees an EMPTY secrets dir — never the
        # gateway's own secrets.
        res = self.core.run({"argv": ["list"]})
        self.assertIn("Nessun account configurato", res["stdout"])

    def test_invalid_command_fails_fast(self) -> None:
        res = self.core.run({"argv": ["--no-such-flag"],
                             "files": {"secrets/email_config.json": self._cfg()}})
        self.assertNotEqual(res["returncode"], 0)
        self.assertIn("usage", res["stderr"].lower())
        self.assertNotIn(SECRET, res["stderr"])

    def test_connection_failure_does_not_echo_the_password(self) -> None:
        cfg = self._cfg(imap_server="127.0.0.1", imap_port=1,
                        smtp_server="127.0.0.1", smtp_port=1)
        res = self.core.run({"argv": ["list"], "timeout": 30,
                             "files": {"secrets/email_config.json": cfg}})
        self.assertNotEqual(res["returncode"], 0)
        self.assertNotIn(SECRET, res["stdout"] + res["stderr"])
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
