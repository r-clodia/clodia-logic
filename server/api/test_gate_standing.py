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
    """Il terzo esito: non sappiamo.

    La cucitura è `_pending_requests` al PLURALE: la tripla
    `agent|instance|verb` non contiene l'argomento, quindi può coprire più
    richieste in coda, e il titolo si verifica su ognuna
    (clodia-platform#232). Gli esiti sotto non cambiano.
    """

    def test_an_unreachable_gateway_is_503_not_403(self):
        def giu(principal, agent, instance, verb):
            raise G._Unavailable("connection refused")

        with patch.object(G, "_pending_requests", giu):
            r = G._standing_error("davide", "clodia", "-", "topic.put")
        self.assertIsNotNone(r)
        self.assertEqual(r.status_code, 503)

    def test_a_refusal_is_403(self):
        with patch.object(G, "_pending_requests", lambda *a: [_walls()]), \
             patch.object(G.topics_client, "open_topic", _topic_ok), \
             patch.object(G.admin, "is_admin", lambda p: True):
            r = G._standing_error("davide", "clodia", "-", "topic.add_participant")
        self.assertEqual(r.status_code, 403)

    def test_standing_returns_none(self):
        with patch.object(G, "_pending_requests", lambda *a: [_walls()]), \
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


class WhatTheCardSaysTests(unittest.TestCase):
    """La card dice cosa attraversa e chi decide (voce 25).

    Prima diceva solo agente, verbo ed età. Chi la guardava non sapeva né
    quale confine stesse per spostare né, se il rifiuto arrivava, a chi
    rivolgersi — e i rimedi qui sono persone diverse: un admin per le regole
    della macchina, l'owner per il confine della sua stanza.

    La tentazione era ricalcolarlo nel frontend a partire dalla classe. Due
    copie della stessa regola divergono, e la copia che diverge è sempre quella
    che SPIEGA: si finirebbe a mostrare «decide un admin» su un gate che solo
    l'owner può sbloccare, mandando la persona sbagliata a cercare un permesso
    che non ha. Per questo `_standing` è una funzione sola, usata sia da chi
    decide sia da chi racconta.
    """

    def test_a_walls_gate_names_the_room_and_its_owner(self):
        with patch.object(G.topics_client, "open_topic", _topic_ok):
            out = G._decorate(_walls(), {})
        self.assertEqual(out["decided_by"], "owner:SEAL-1/proof-of-flex")
        self.assertEqual(out["decider_name"], "giovanni")
        self.assertIn("confine", out["crosses"])

    def test_leaving_the_room_is_said_differently_from_moving_its_wall(self):
        """Sono due atti diversi e la card non deve confonderli: uno allarga chi
        può entrare, l'altro fa uscire i dati."""
        muro = G._decorate(_walls(), {})
        fuori = G._decorate({"verb": "web.post", "class": "outward",
                             "chat": "chan:SEAL-1:proof-of-flex:clodia"}, {})
        self.assertNotEqual(muro["crosses"], fuori["crosses"])
        self.assertIn("uscita", fuori["crosses"])

    def test_a_system_gate_says_admin_and_names_no_room(self):
        out = G._decorate(_system(), {})
        self.assertEqual(out["decided_by"], "admin")
        self.assertNotIn("scope", out)

    def test_an_unreadable_topic_gives_no_name_rather_than_a_wrong_one(self):
        """Nessun nome è un'informazione; un nome sbagliato manda a bussare
        alla porta di qualcun altro."""
        def rotto(tier, name):
            raise RuntimeError("gateway giù")

        with patch.object(G.topics_client, "open_topic", rotto):
            out = G._decorate(_walls(), {})
        self.assertEqual(out["decider_name"], "")
        self.assertEqual(out["decided_by"], "owner:SEAL-1/proof-of-flex")

    def test_the_owner_is_read_once_per_room(self):
        """Dieci gate nella stessa stanza non sono dieci letture del topic."""
        letture = []

        def conta(tier, name):
            letture.append((tier, name))
            return META and {"meta": META}

        cache: dict = {}
        with patch.object(G.topics_client, "open_topic", conta):
            for _ in range(5):
                G._decorate(_walls(), cache)
        self.assertEqual(len(letture), 1)

    def test_the_explanation_and_the_decision_come_from_one_rule(self):
        """Il test che tiene insieme le due metà: se un giorno `_may_decide`
        cambiasse senza `_standing`, la card racconterebbe una regola che non è
        più quella applicata."""
        import inspect
        self.assertIn("_standing(req)", inspect.getsource(G._may_decide))


class StandingShapeTests(unittest.TestCase):
    def test_the_decider_is_an_identifier_not_a_sentence(self):
        """Chi legge deve poterlo confrontare. Una frase italiana si riformula
        il giorno dopo e ogni confronto smette di funzionare."""
        chi, _, _ = G._standing(_walls())
        self.assertEqual(chi, "owner:SEAL-1/proof-of-flex")
        self.assertEqual(G._standing(_system())[0], "admin")

    def test_an_unclassified_gate_does_not_invent_a_scope(self):
        chi, cosa, dove = G._standing({"verb": "x", "class": None, "chat": None})
        self.assertEqual(chi, "admin")
        self.assertEqual(dove, "")
        self.assertIn("non ha classificato", cosa)


class TheWatcherSaysSoTests(unittest.TestCase):
    """Una presenza legittima che sembra un'intrusione.

    Il 10 ago 2026 Davide ha visto `sysadmin` — non partecipante di quel
    canale — chiedere un gate in una sua stanza. Era il guardiano della
    modalità debug, svegliato da un turno fallito lì: comportamento voluto, e
    del tutto illeggibile sullo schermo.

    Costa quanto un'intrusione finché qualcuno non la spiega — e la persona che
    guarda la card non ha i log sotto mano. Ora la card lo dice.
    """

    def _req(self, agent):
        return {"agent": agent, "verb": "egress:github:https://x/y",
                "class": "outward", "chat": "chan:SEAL-1:proof-of-flex:clodia"}

    def test_the_watcher_is_labelled(self):
        with patch.object(G, "_is_watcher", lambda a: a == "sysadmin"), \
             patch.object(G.topics_client, "open_topic", _topic_ok):
            out = G._decorate(self._req("sysadmin"), {})
        self.assertEqual(out["asker_role"], "debug-watcher")
        self.assertIn("non è un partecipante", out["asker_note"])

    def test_an_ordinary_agent_is_not(self):
        """L'etichetta deve essere rara: se comparisse su tutti smetterebbe di
        dire qualcosa."""
        with patch.object(G, "_is_watcher", lambda a: a == "sysadmin"), \
             patch.object(G.topics_client, "open_topic", _topic_ok):
            out = G._decorate(self._req("messaggero"), {})
        self.assertNotIn("asker_role", out)

    def test_with_debug_off_nobody_is_the_watcher(self):
        """Fuori dalla modalità debug il guardiano non esiste, e `sysadmin` che
        chiede un gate è un agente come gli altri — etichettarlo lo stesso
        direbbe una cosa falsa."""
        from .. import debug_watch
        with patch.object(debug_watch, "enabled", lambda: False):
            self.assertFalse(G._is_watcher(debug_watch.WATCHER))

    def test_the_label_does_not_change_who_decides(self):
        """Dire chi chiede non sposta il titolo: resta l'owner della stanza."""
        with patch.object(G, "_is_watcher", lambda a: True), \
             patch.object(G.topics_client, "open_topic", _topic_ok):
            out = G._decorate(self._req("sysadmin"), {})
        self.assertTrue(out["decided_by"].startswith("owner:"))
