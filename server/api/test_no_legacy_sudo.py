from __future__ import annotations

import unittest

from .. import main


class NoLegacySudoTests(unittest.TestCase):
    def test_public_sudo_routes_are_absent(self) -> None:
        paths = {
            getattr(route, "path", "")
            for route in main.create_app().routes
        }
        self.assertFalse(any(path.startswith("/api/sudo") for path in paths))


if __name__ == "__main__":
    unittest.main()
