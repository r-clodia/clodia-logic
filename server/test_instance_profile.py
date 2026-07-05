"""Test del profilo d'istanza (Modular Distro F1)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import instance_profile as ip


class InstanceProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._old_data_path = ip.data_path
        root = Path(self.tmp.name)
        ip.data_path = lambda rel: root / rel  # type: ignore[assignment]
        ip._CACHE = None

    def tearDown(self) -> None:
        ip.data_path = self._old_data_path  # type: ignore[assignment]
        ip._CACHE = None
        self.tmp.cleanup()

    def _write(self, text: str) -> None:
        (Path(self.tmp.name) / ip.PROFILE_FILENAME).write_text(text, encoding="utf-8")

    def test_absent_file_is_full(self) -> None:
        p = ip.load(force=True)
        self.assertEqual(p.edition, "full")
        self.assertTrue(p.features.jobs)
        self.assertEqual(p.features.topics, "full")
        self.assertEqual(p.features.rag, "full")
        self.assertEqual(p.features.integrations, "full")
        self.assertTrue(p.features.channels)

    def test_jobs_only_edition(self) -> None:
        self._write(
            "edition: acme-jobs\n"
            "features:\n"
            "  jobs: true\n"
            "  topics: single\n"
            "  rag: single\n"
            "  integrations: fixed\n"
            "  channels: false\n"
            "rag: {collection: acme-kb}\n"
            "integrations: {allowed: [normattiva]}\n"
            "branding: {name: ACME Agency, accent: '#1a5fb4'}\n"
        )
        p = ip.load(force=True)
        self.assertEqual(p.edition, "acme-jobs")
        self.assertEqual(p.features.topics, "single")
        self.assertFalse(p.features.channels)
        self.assertEqual(p.rag.collection, "acme-kb")
        self.assertEqual(p.integrations.allowed, ["normattiva"])
        view = ip.public_view()
        self.assertEqual(view["branding"]["name"], "ACME Agency")
        self.assertEqual(view["rag"], {"collection": "acme-kb"})
        self.assertEqual(view["topics_single"], {"name": "workspace", "tier": "SEAL-1"})

    def test_yaml_off_unquoted_is_tristate_off(self) -> None:
        # Gotcha YAML 1.1: `off` non quotato = booleano False → mappato a "off".
        self._write("features:\n  topics: off\n  rag: off\n")
        p = ip.load(force=True)
        self.assertEqual(p.features.topics, "off")
        self.assertEqual(p.features.rag, "off")

    def test_invalid_file_falls_back_full_with_warning(self) -> None:
        self._write("features:\n  topics: banana\n")
        with self.assertLogs("agent-server.instance_profile", level="ERROR"):
            p = ip.load(force=True)
        self.assertEqual(p.features.topics, "full")   # fallback FULL

    def test_unknown_field_rejected_to_full(self) -> None:
        self._write("features: {jetpack: true}\n")
        with self.assertLogs("agent-server.instance_profile", level="ERROR"):
            p = ip.load(force=True)
        self.assertEqual(p.edition, "full")

    def test_cache_and_force(self) -> None:
        p1 = ip.load(force=True)
        self._write("edition: nuova\n")
        self.assertIs(ip.load(), p1)                 # cache
        self.assertEqual(ip.load(force=True).edition, "nuova")


if __name__ == "__main__":
    unittest.main()
