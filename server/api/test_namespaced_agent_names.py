"""`namespace.shortname` è un nome di seed valido (router-notebook R18, 12 set 2026).

Un seed derivato con `parents:` prende lo stesso shortname del genitore, e il
namespace lo distingue quando più business ne derivano uno ciascuno
(`tomato.fullstack-dev` vs `uncommon.fullstack-dev`). Un solo livello di
punto — la stessa forma dichiarata in `mentions.py::_NAME`, perché il tag
`@tomato.fullstack-dev` deve essere riconosciuto come UNA mention da chi
instrada i turni.
"""
from __future__ import annotations

import unittest

from . import pack_import


class NamespacedNameTests(unittest.TestCase):
    def test_a_namespaced_name_is_accepted(self):
        self.assertTrue(pack_import._AGENT_NAME_RE.fullmatch("tomato.fullstack-dev"))

    def test_a_bare_name_is_still_accepted(self):
        """Non-regressione: la stragrande maggioranza dei seed non ha
        namespace, e non deve smettere di validare."""
        self.assertTrue(pack_import._AGENT_NAME_RE.fullmatch("fullstack-dev"))

    def test_two_levels_of_dot_are_not_a_valid_name(self):
        """Un solo livello di namespacing: `a.b.c` non è mai stato dichiarato
        e non deve passare silenziosamente."""
        self.assertIsNone(pack_import._AGENT_NAME_RE.fullmatch("a.b.c"))

    def test_a_trailing_or_leading_dot_is_not_valid(self):
        self.assertIsNone(pack_import._AGENT_NAME_RE.fullmatch("tomato."))
        self.assertIsNone(pack_import._AGENT_NAME_RE.fullmatch(".fullstack-dev"))

    def test_an_empty_shortname_after_the_dot_is_not_valid(self):
        self.assertIsNone(pack_import._AGENT_NAME_RE.fullmatch("tomato.-"))


if __name__ == "__main__":
    unittest.main()
