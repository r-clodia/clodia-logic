"""Tests for composing and inheriting the origin chain (specification.md §3.3).

What has to hold is one thing: **a delegation inherits, it does not restart**.
Restarting the chain at each hop is precisely how authority gets amplified —
Giovanni asks clodia, clodia asks messaggero, and if the second hop begins a fresh
chain the gateway sees only `agent:clodia → agent:messaggero` and Giovanni's
missing permission never enters the decision.

The router only TRANSPORTS the chain. It runs inside the agents' container, so a
defect here must not be an authorisation bypass — the gateway intersects, and a
chain that arrives wrong is a chain that arrives *narrower or unknown*, never
wider.
"""
from __future__ import annotations

import unittest

from . import channels


class ComposeTests(unittest.TestCase):
    def test_a_human_turn_starts_from_the_human(self):
        self.assertEqual(channels._origin_for("giovanni", None, "clodia"),
                         ["human:giovanni", "agent:clodia"])

    def test_a_delegation_inherits_and_appends(self):
        """Il caso che il modello esiste per coprire."""
        self.assertEqual(
            channels._origin_for("giovanni", ["human:giovanni", "agent:clodia"],
                                 "messaggero"),
            ["human:giovanni", "agent:clodia", "agent:messaggero"])

    def test_a_delegation_does_not_restart_the_chain(self):
        """Formulato come proprietà, non come uguaglianza: qualunque catena
        ereditata deve restare un PREFISSO di quella prodotta. È l'invariante che
        impedisce l'amplificazione."""
        inherited = ["human:giovanni", "agent:clodia"]
        out = channels._origin_for("giovanni", inherited, "messaggero")
        self.assertEqual(out[:len(inherited)], inherited)
        self.assertIn("human:giovanni", out)

    def test_no_human_link_is_invented_when_there_is_no_human(self):
        """`principal` vale "channel" per un innesco interno. Inventare un anello
        umano lì significherebbe attribuire a una persona un'azione che non ha
        chiesto — e, se quella persona è un admin, allargare la catena."""
        self.assertEqual(channels._origin_for("channel", None, "sysadmin"),
                         ["agent:sysadmin"])
        self.assertEqual(channels._origin_for("feedback", None, "clodia"),
                         ["agent:clodia"])

    def test_the_executor_is_not_duplicated_on_a_reused_session(self):
        self.assertEqual(
            channels._origin_for("giovanni", ["human:giovanni", "agent:clodia"],
                                 "clodia"),
            ["human:giovanni", "agent:clodia"])

    def test_the_inherited_list_is_not_mutated(self):
        """Le deleghe in parallelo condividono la catena del delegante: mutarla
        farebbe comparire in una catena l'esecutore di un'altra."""
        inherited = ["human:giovanni", "agent:clodia"]
        channels._origin_for("giovanni", inherited, "messaggero")
        channels._origin_for("giovanni", inherited, "segretario")
        self.assertEqual(inherited, ["human:giovanni", "agent:clodia"])


class WiringTests(unittest.TestCase):
    """La catena deve ARRIVARE al token, non solo essere calcolata."""

    def test_start_turn_sets_it_on_the_session(self):
        import inspect
        src = inspect.getsource(channels._start_turn)
        self.assertIn("chat.origin = _origin_for(", src)

    def test_the_delegation_passes_the_parents_chain(self):
        import inspect
        src = inspect.getsource(channels._maybe_delegate)
        self.assertIn("origin_chain", src)
        # e i chiamanti la prendono dalla sessione appena conclusa
        mod = inspect.getsource(channels)
        self.assertIn('origin_chain=getattr(chat, "origin", None)', mod)

    def test_the_token_carries_it_signed(self):
        """Firmata, o l'intersezione sarebbe la parola dell'agente su sé stesso."""
        import inspect
        from ..colony import pki
        src = inspect.getsource(pki.mint_session_token)
        i_payload = src.find('payload["origin"]')
        i_sign = src.find("key.sign(")
        self.assertGreater(i_payload, 0, "la catena non entra nel payload")
        self.assertGreater(i_sign, i_payload, "deve entrare PRIMA della firma")

    def test_the_session_forwards_it_to_every_mint_site(self):
        import pathlib
        src = pathlib.Path(channels.__file__).parent.parent \
            .joinpath("sdk_runtime", "session.py").read_text(encoding="utf-8")
        self.assertEqual(src.count('origin=getattr(self, "origin", None)'),
                         src.count("chat=self.chat_id,"),
                         "un punto di minting senza la catena manderebbe un turno "
                         "senza origine, e il gateway lo tratterebbe come "
                         "sconosciuto")


if __name__ == "__main__":
    unittest.main()
