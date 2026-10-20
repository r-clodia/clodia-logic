"""#511 — no credential from a query string in the access log."""
from __future__ import annotations

import logging
import unittest

from . import log_redact


class RedactTests(unittest.TestCase):
    def test_sensitive_parameters_are_masked(self):
        cases = {
            "/clodia/events?token=ckt1.abc.def": "/clodia/events?token=***",
            "/x?a=1&access_token=zz&b=2": "/x?a=1&access_token=***&b=2",
            "/cb?code=4/0Ab&state=s": "/cb?code=***&state=s",
            "/x?api_key=K&API-KEY=K2": "/x?api_key=***&API-KEY=***",
            "/x?client_secret=S": "/x?client_secret=***",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(log_redact.redact(raw), want)

    def test_ordinary_parameters_stay(self):
        for raw in ("/topics?tier=SEAL-1&name=x", "/files?path=local%2Fa.pdf", "/x?tokenize=1"):
            with self.subTest(raw=raw):
                self.assertEqual(log_redact.redact(raw), raw)

    def test_the_filter_rewrites_uvicorns_access_record(self):
        rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                '%s - "%s %s HTTP/%s" %d',
                                ("1.2.3.4:5", "GET", "/clodia/events?token=ckt1.secret", "1.1", 200),
                                None)
        self.assertTrue(log_redact.RedactQueryFilter().filter(rec))
        self.assertNotIn("ckt1.secret", rec.getMessage())
        self.assertIn("token=***", rec.getMessage())

    def test_install_is_idempotent(self):
        lg = logging.getLogger("test.access.511")
        log_redact.install(("test.access.511",))
        log_redact.install(("test.access.511",))
        self.assertEqual(sum(isinstance(f, log_redact.RedactQueryFilter) for f in lg.filters), 1)


if __name__ == "__main__":
    unittest.main()
