"""Il confine si verifica, non si assume.

Invariante 8, e fino all'8 ago 2026 **scritta e non asserita da nulla** — proprio
quella che più ne aveva bisogno, perché non poggia su codice nostro ma sui
permessi delle directory, cioè su come l'istanza è montata.

**Era anche formulata male, e misurarla l'ha dimostrato.** Diceva «l'agent-server
non vede il topic store», misurato una volta sullo stack personale dove una
maschera di compose lo nasconde. Su venere quella riga non c'è: il processo gira
come root e il vault lo vede. Un test scritto su quella formulazione sarebbe
diventato rosso mandando a inseguire una differenza di configurazione invece di
una proprietà di sicurezza.

La proprietà che conta vale su entrambe le istanze: **uno SPAWN non raggiunge il
vault, il topic store, né i seed** — misurato su venere, tutto negato. E il
confine lo mette il kernel (`drwx------ root` contro un uid unprivileged), che è
l'unico tipo di confine che non si perde in un aggiornamento del compose.

Qui si testa la LOGICA della verifica; la verifica vera gira al boot dentro il
container, che è il solo posto dove quei permessi esistono.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import boundary_check as B


class ThreeOutcomesTests(unittest.TestCase):
    """«Non so» non è «al sicuro», e i tre esiti restano distinti."""

    def test_a_reachable_path_is_reported_as_broken(self):
        with patch.object(B.os, "geteuid", lambda: 0), \
             patch.object(B, "_spawn_uid", lambda: 60000), \
             patch.object(B, "_raggiungibile", lambda p, u: True), \
             self.assertLogs("agent-server.agents.boundary", "ERROR") as log:
            esiti = B.check()
        self.assertTrue(all(v is True for v in esiti.values()))
        self.assertIn("CONFINE ROTTO", "".join(log.output))

    def test_an_unverifiable_path_is_not_reported_as_safe(self):
        with patch.object(B.os, "geteuid", lambda: 0), \
             patch.object(B, "_spawn_uid", lambda: 60000), \
             patch.object(B, "_raggiungibile", lambda p, u: None), \
             self.assertLogs("agent-server.agents.boundary", "WARNING") as log:
            esiti = B.check()
        self.assertTrue(all(v is None for v in esiti.values()))
        self.assertIn("non verificabile", "".join(log.output))

    def test_a_denied_path_is_the_expected_state(self):
        with patch.object(B.os, "geteuid", lambda: 0), \
             patch.object(B, "_spawn_uid", lambda: 60000), \
             patch.object(B, "_raggiungibile", lambda p, u: False):
            self.assertTrue(all(v is False for v in B.check().values()))


class WhatIsGuardedTests(unittest.TestCase):
    def test_the_vault_the_topic_store_and_the_seeds(self):
        """I tre posti che uno spawn non deve raggiungere: le credenziali, i
        dati di OGNI topic, e l'autorità stessa."""
        for p in ("/datadir/clodia-vault", "/datadir/clodia-vault/topics-store",
                  "/datadir/agents"):
            with self.subTest(path=p):
                self.assertIn(p, B.VIETATI)

    def test_every_guarded_path_says_why(self):
        """Un elenco di path senza il motivo non si mantiene: alla prima
        modifica nessuno sa se una voce può uscire."""
        for p, perche in B.VIETATI.items():
            with self.subTest(path=p):
                self.assertTrue(perche and len(perche) > 20)


class SpawnUidTests(unittest.TestCase):
    def test_the_uid_is_read_from_a_real_spawn(self):
        """Verificare il confine per un utente che non esiste passerebbe per la
        ragione sbagliata."""
        import inspect
        self.assertIn("os.stat", inspect.getsource(B._spawn_uid))

    def test_it_falls_back_when_no_spawn_exists(self):
        with patch.object(B.os, "listdir", side_effect=OSError("vuoto")):
            self.assertEqual(B._spawn_uid(), B.SPAWN_UID)


class NotRootTests(unittest.TestCase):
    def test_without_root_it_says_so_instead_of_passing(self):
        """Senza root non si può assumere l'uid di uno spawn: dire «verificato»
        sarebbe la bugia peggiore che questo file possa raccontare."""
        with patch.object(B.os, "geteuid", lambda: 1000):
            self.assertEqual(B.check(), {})


class WiringTests(unittest.TestCase):
    def test_it_runs_at_boot(self):
        import inspect
        from .. import main as M
        self.assertIn("boundary_check", inspect.getsource(M))


if __name__ == "__main__":
    unittest.main()
