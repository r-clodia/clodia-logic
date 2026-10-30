"""#640 — a declared service's code reaches `<datadir>/packs/<pack>/<workdir>`.

The gateway starts a pack service in that directory (#515); until #640 the
importer wrote only `pack.yaml` there, so every imported service was refused
as «workdir is not a directory».
"""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import gateway_admin, pack_import
from .test_519_connector_validation import CONNETTORE, _Install

SERVIZIO = {**CONNETTORE, "services": [{
    "name": "listener", "command": ["node", "service.mjs"],
    "workdir": "plugins/plugin-svc/services/listener",
    "health": {"kind": "http", "target": "http://127.0.0.1:8099/health"}}]}

_OK = {"return_value": {"ok": True, "connectors": [{"id": "citofono", "ok": True}]}}


class ServiceCodeTests(_Install):
    def _pack(self, nome="svc", *, extra=None):
        d = self._pack_root(nome, connectors=(SERVIZIO,))
        # the plugin's directory name must match the workdir above
        (d / "plugins" / f"plugin-{nome}").rename(d / "plugins" / "plugin-svc") \
            if nome != "svc" else None
        code = d / "plugins" / "plugin-svc" / "services" / "listener"
        code.mkdir(parents=True)
        (code / "service.mjs").write_text("console.log('hi')\n")
        (code / "lib").mkdir()
        (code / "lib" / "util.mjs").write_text("export const x = 1\n")
        if extra:
            extra(code)
        return d

    def _import(self, d):
        with patch.object(gateway_admin, "connectors_validate", **_OK):
            return pack_import.install_pack_from_root(d, source="test")

    def test_the_code_lands_in_the_workdir_readable_not_writable(self):
        self._import(self._pack())
        dest = self.packs_meta / "svc" / "plugins" / "plugin-svc" / "services" / "listener"
        self.assertEqual((dest / "service.mjs").read_text(), "console.log('hi')\n")
        self.assertTrue((dest / "lib" / "util.mjs").is_file())
        self.assertEqual(stat.S_IMODE(os.stat(dest / "service.mjs").st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(dest.parent).st_mode), 0o755)

    def test_a_reimport_replaces_the_code(self):
        d = self._pack()
        self._import(d)
        code = d / "plugins" / "plugin-svc" / "services" / "listener"
        (code / "service.mjs").write_text("console.log('v2')\n")
        (code / "lib" / "util.mjs").unlink()
        self._import(d)
        dest = self.packs_meta / "svc" / "plugins" / "plugin-svc" / "services" / "listener"
        self.assertEqual((dest / "service.mjs").read_text(), "console.log('v2')\n")
        self.assertFalse((dest / "lib" / "util.mjs").exists())
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["listener"])

    def test_a_link_fails_the_import_with_nothing_installed(self):
        d = self._pack(extra=lambda code: os.symlink("/etc/passwd", code / "evil"))
        with self.assertRaises(pack_import.PackImportError):
            self._import(d)
        self.assertEqual(self._installato("svc"), {})
        self.assertFalse((self.packs_meta / "svc" / "plugins").exists())

    def test_a_tree_over_the_limit_fails(self):
        d = self._pack()
        with patch.object(pack_import, "_SERVICE_MAX_FILES", 1):
            with self.assertRaises(pack_import.PackImportError):
                self._import(d)
        self.assertEqual(self._installato("svc"), {})

    def test_a_workdir_outside_or_missing_fails(self):
        for wd in ("../escape", "/abs", "plugins/nope"):
            with self.subTest(wd=wd):
                bad = {**SERVIZIO, "services": [{**SERVIZIO["services"][0], "workdir": wd}]}
                d = self._pack_root(f"bad{abs(hash(wd)) % 1000}", connectors=(bad,))
                with self.assertRaises(pack_import.PackImportError):
                    self._import(d)

    def test_a_pack_without_services_is_unchanged(self):
        d = self._pack_root("plain")
        self._import(d)
        self.assertEqual(sorted(p.name for p in (self.packs_meta / "plain").iterdir()
                                if not p.name.startswith(".")), ["pack.yaml"])


if __name__ == "__main__":
    unittest.main()
