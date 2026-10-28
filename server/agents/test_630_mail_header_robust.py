"""One mail with an unknown charset must not fail a whole listing (28 Oct
2026: the colony mailbox's index returned 502 every cycle)."""
from __future__ import annotations

import email
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "catalogs" / "packs"
CLIENTS = [ROOT / "google-pack" / "gateway" / "email_client.py",
           ROOT / "comms-pack" / "gateway" / "email_client.py"]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BAD = (b"From: =?x-unknown?q?Mittente?= <a@b.it>\r\n"
       b"To: box+R5WYS@gmail.com\r\nSubject: =?utf-8'?q?Fattura?=\r\n"
       b"Message-ID: <1@b.it>\r\n\r\n")


class HeaderTests(unittest.TestCase):
    def test_unknown_charsets_decode_without_raising(self):
        for i, path in enumerate(CLIENTS):
            with self.subTest(client=path.parent.parent.name):
                m = _load(path, f"_ec630_{i}")
                self.assertEqual(m.decode_mime_header("=?x-unknown?q?Fattura?="), "Fattura")
                row = m._header_row(b"7", email.message_from_bytes(BAD))
                self.assertEqual(row["id"], "7")
                self.assertIn("box+R5WYS@gmail.com", " ".join(row["rcpt_raw"]))


if __name__ == "__main__":
    unittest.main()
