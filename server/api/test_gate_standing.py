"""Chi ha titolo a sbloccare un gate.

Voce 24: «quando si attraversa il confine dello scope si attiva il gate, ed in
questo caso è l'owner del gate a sbloccare o negare». Fino al 7 ago 2026 la
regola era una sola riga — `admin.is_admin(principal)` — applicata a ogni
classe. Due conseguenze misurate:

  1. un gate che sposta il confine di `proof-of-flex` lo approvava un admin
     qualunque della piattaforma, non Giovanni che ne è l'owner. L'autorità
     dell'owner era dichiarata e nessuno la portava — il difetto ricorrente di
     questa settimana, trovato sette volte;
  2. `deny` non aveva alcun controllo. Chiunque autenticato poteva negare il
     gate di chiunque: non una fuga di dati, il modo più economico per fermare
     il lavoro altrui.

La classe del gate e la stanza in cui l'azione è nata arrivano dal GATEWAY, non
dal body: chiedere a chi approva in quale stanza si trovava l'azione da
approvare sarebbe la parola di chi chiede su dove si trova.

E il terzo esito. Se il gateway non risponde, la risposta è 503, non 403: un
guasto travestito da rifiuto manda a chiedere alla persona sbagliata, ed è
esattamente ciò che è costato tre diagnosi il 6 agosto.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import gate as G


META = {"owner": "giovanni", "participants": {"matteo": "contributor"}}


def _topic_ok(tier, name):
    return {"meta": META}


def _walls(chat="chan:SEAL-1:proof-of-flex:clodia"):
    return {"verb": "topic.add_participant", "class": "walls", "chat": chat}


def _system():
    return {"verb": "agents.grant_tool", "class": "system", "chat": None}


class WallsTests(unittest.TestCase):
    """Il confine di una stanza lo sposta chi la possiede."""

    def setUp(self):
        self.t = patch.object(G.topics_client, "open_topic", _topic_ok)
        self.t.start()
        self.addCleanup(self.t.stop)

    def test_the_scope_owner_decides(self):
        with patch.object(G.admin, "is_admin", lambda p: False):
            ok, _ = G._may_decide("giovanni", _walls())
            self.assertTrue(ok)

    def test_a_platform_admin_does_not_substitute_the_owner(self):
        """Se un admin potesse approvare al posto suo, l'autorità dell'owner
        sarebbe decorativa. Un admin può ancora cambiare il topic dalla porta
        principale: la differenza è che lì la cosa ha un nome e un log."""
        with patch.object(G.admin, "is_admin", lambda p: True):
            ok, motivo = G._may_decide("davide", _walls())
            self.assertFalse(ok)
            self.assertIn("owner di quel topic", motivo)

    def test_a_participant_is_not_an_owner(self):
        with patch.object(G.admin, "is_admin", lambda p: False):
            ok, _ = G._may_decide("matteo", _walls())
            self.assertFalse(ok)

    def test_the_refusal_names_the_topic_and_the_person_to_ask(self):
        """Un rifiuto che non indica la strada insegna solo che il sistema dice
        di no — e i rimedi qui sono persone diverse."""
        with patch.object(G.admin, "is_admin", lambda p: True):
            _, motivo = G._may_decide("davide", _walls())
            self.assertIn("proof-of-flex", motivo)
            self.assertIn("owner", motivo)


class OutwardTests(unittest.TestCase):
    def test_leaving_the_room_is_the_owners_call_too(self):
        """Uscire porta fuori i dati della stanza: è il suo confine."""
        with patch.object(G.topics_client, "open_topic", _topic_ok), \
             patch.object(G.admin, "is_admin", lambda p: True):
            req = {"verb": "web.post", "class": "outward",
                   "chat": "chan:SEAL-1:proof-of-flex:clodia"}
            self.assertTrue(G._may_decide("giovanni", req)[0])
            self.assertFalse(G._may_decide("davide", req)[0])


class SystemTests(unittest.TestCase):
    """Le regole della macchina non sono di nessuna stanza."""

    def test_an_admin_decides(self):
        with patch.object(G.admin, "is_admin", lambda p: p == "davide"):
            self.assertTrue(G._may_decide("davide", _system())[0])

    def test_someone_who_is_not_an_admin_does_not(self):
        with patch.object(G.admin, "is_admin", lambda p: False):
            ok, motivo = G._may_decide("giovanni", _system())
            self.assertFalse(ok)
            self.assertIn("delegare", motivo)

    def test_owning_a_topic_does_not_grant_system_gates(self):
        """L'altra direzione: possedere una stanza non dà le chiavi della
        macchina. Intersezione, mai unione."""
        with patch.object(G.topics_client, "open_topic", _topic_ok), \
             patch.object(G.admin, "is_admin", lambda p: False):
            self.assertFalse(G._may_decide("giovanni", _system())[0])


class OutsideAScopeTests(unittest.TestCase):
    def test_without_a_room_there_is_no_owner_to_call_upon(self):
        """Turno di un job, gate non presidiato: decide un admin, come prima."""
        with patch.object(G.admin, "is_admin", lambda p: p == "davide"):
            req = {"verb": "topic.add_participant", "class": "walls", "chat": None}
            self.assertTrue(G._may_decide("davide", req)[0])

    def test_an_unreadable_topic_does_not_make_anyone_an_owner(self):
        """Fail-closed: degradare ad autorizzato su un errore di lettura
        trasformerebbe un guasto in un permesso."""
        def rotto(tier, name):
            raise RuntimeError("gateway giù")

        with patch.object(G.topics_client, "open_topic", rotto), \
             patch.object(G.admin, "is_admin", lambda p: True):
            self.assertFalse(G._may_decide("giovanni", _walls())[0])


class AuthoritativeSourceTests(unittest.TestCase):
    def test_the_room_is_not_taken_from_the_body(self):
        """Se `chat` venisse dal body, chi approva direbbe da sé quale confine
        sta attraversando l'azione che approva."""
        import inspect
        src = inspect.getsource(G.approve)
        self.assertIn("_standing_error", src)
        self.assertNotIn('body.get("class")', src)

    def test_the_class_is_not_recomputed_here(self):
        """L'autorità sulla classificazione è il gateway. Riderivarla qui
        sarebbe una regola duplicata, e una regola duplicata diverge."""
        import inspect
        self.assertNotIn("_GATE_CLASS", inspect.getsource(G))


class UnavailableTests(unittest.TestCase):
    """Il terzo esito: non sappiamo."""

    def test_an_unreachable_gateway_is_503_not_403(self):
        def giu(principal, agent, instance, verb):
            raise G._Unavailable("connection refused")

        with patch.object(G, "_pending_request", giu):
            r = G._standing_error("davide", "clodia", "-", "topic.put")
        self.assertIsNotNone(r)
        self.assertEqual(r.status_code, 503)

    def test_a_refusal_is_403(self):
        with patch.object(G, "_pending_request", lambda *a: _walls()), \
             patch.object(G.topics_client, "open_topic", _topic_ok), \
             patch.object(G.admin, "is_admin", lambda p: True):
            r = G._standing_error("davide", "clodia", "-", "topic.add_participant")
        self.assertEqual(r.status_code, 403)

    def test_standing_returns_none(self):
        with patch.object(G, "_pending_request", lambda *a: _walls()), \
             patch.object(G.topics_client, "open_topic", _topic_ok):
            self.assertIsNone(
                G._standing_error("giovanni", "clodia", "-", "topic.add_participant"))


class DenyTests(unittest.TestCase):
    def test_denying_is_guarded_by_the_same_standing(self):
        """Negare è una decisione sulla richiesta di qualcun altro. Prima di
        oggi non aveva alcun controllo."""
        import inspect
        self.assertIn("_standing_error", inspect.getsource(G.deny))


class ScopeParsingTests(unittest.TestCase):
    def test_a_channel_yields_its_topic(self):
        self.assertEqual(G._scope_of("chan:SEAL-1:acme:clodia"), ("SEAL-1", "acme"))

    def test_a_job_is_not_a_room(self):
        self.assertIsNone(G._scope_of("job:42"))
        self.assertIsNone(G._scope_of(None))
        self.assertIsNone(G._scope_of("chan:SEAL-1"))


if __name__ == "__main__":
    unittest.main()
