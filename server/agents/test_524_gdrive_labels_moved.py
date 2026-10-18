"""Etichette delle cartelle Drive nelle whitelist (clodia-platform#424).

On google-pack's gateway component (moved from clodia-tools
`server/tools/test_gdrive_labels.py`, clodia-platform#524).
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from .google_fake_524 import ServiceTransport

from .google_moved_524 import gdrive, google_io, rebind
from .google_moved_524 import gdrive_labels as L

FID = "1xxBOdhf4Vz2lgHAsxIBOEfyY9ymJn_NT"
URI = f"gdrive:folder/{FID}"


class LabelTests(unittest.TestCase):
    def setUp(self):
        L._cache.clear()
        L._inflight.clear()

    def test_the_link_is_always_there_and_the_name_comes_from_drive(self):
        with patch.object(L, "_lookup", side_effect=lambda fid: (L._store(fid, "Progetto HEDGE"), "Progetto HEDGE")[1]):
            out = L.labels([URI, "mailto:x@y.it", "tg:-5411149155"])
        self.assertEqual(list(out), [URI], "solo le voci gdrive:folder/ hanno un'etichetta")
        self.assertEqual(out[URI]["url"], f"https://drive.google.com/drive/folders/{FID}")
        self.assertEqual(out[URI]["name"], "Progetto HEDGE")

    def test_a_slow_drive_does_not_hold_the_list(self):
        def lento(fid):
            time.sleep(2)
            L._store(fid, "tardi")
            return "tardi"
        with patch.object(L, "_lookup", side_effect=lento):
            t0 = time.time()
            out = L.labels([URI], budget_s=0.2)
            self.assertLess(time.time() - t0, 1.5)
        self.assertIsNone(out[URI]["name"])
        self.assertTrue(out[URI]["url"].endswith(FID))

    def test_a_cached_name_is_served_without_asking_drive(self):
        L._store(FID, "In cache")
        with patch.object(L, "_lookup", side_effect=AssertionError("non va chiamato")):
            self.assertEqual(L.labels([URI])[URI]["name"], "In cache")

    def test_a_drive_error_falls_back_to_the_link(self):
        class _Svc:
            def files(self):
                raise RuntimeError("403 insufficient permissions")
        rebind()
        with patch.object(gdrive, "gworkspace_accounts", return_value=["davide"]), \
                patch.object(google_io, "_transport", ServiceTransport(_Svc())):
            out = L.labels([URI])
        self.assertIsNone(out[URI]["name"])
        self.assertIn(FID, out[URI]["url"])

    def test_degenerate_entries_get_no_label(self):
        self.assertEqual(L.labels(["gdrive:folder/", "gdrive:folder/../x"]), {})


if __name__ == "__main__":
    unittest.main()
