from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import gateway_admin


class DeniedToolsTransportTests(unittest.TestCase):

    def test_declared_denies_are_sent_to_the_gateway(self) -> None:
        posted = {}

        def post(_url, **kwargs):
            posted.update(kwargs["json"])
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"ok": True})

        with patch.object(gateway_admin, "_headers", return_value={}), \
             patch.object(gateway_admin.requests, "post", side_effect=post):
            gateway_admin.register_agent(
                "messaggero", ["email.*"],
                denied_tools=["topic.files", "topic.read_file"],
            )

        self.assertEqual(posted["denied_tools"],
                         ["topic.files", "topic.read_file"])

    def test_absent_denies_are_omitted(self) -> None:
        posted = {}

        def post(_url, **kwargs):
            posted.update(kwargs["json"])
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"ok": True})

        with patch.object(gateway_admin, "_headers", return_value={}), \
             patch.object(gateway_admin.requests, "post", side_effect=post):
            gateway_admin.register_agent("legacy", ["topic.open"])

        self.assertNotIn("denied_tools", posted)


if __name__ == "__main__":
    unittest.main()
