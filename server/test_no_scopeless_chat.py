"""Nessuno spawn vive fuori da uno scope.

Punto aperto 4 del notebook — «cos'è `DEFAULT_CHAT_ID` all'avvio, ed è uno scope
di spawn?» — misurato il 7 ago 2026 sul server in esecuzione:

    chat_id "default" · agent clodia · spawn clodia-1 · context_kind "chat"
    topic: null · state idle · protetta dall'idle-reaper

Quindi sì, era un terzo residuo: la voce 6 dice che uno spawn vive in esattamente
due scope, canale e job, e quella sessione non era né l'uno né l'altro.

**La conseguenza pesa più della tassonomia.** Misurato dentro quella chat: nessun
canale corrente, quindi solo la lista globale in vigore, perimetro vuoto —
nessun mittente fidato per appartenenza — nessun ruolo di scope e nessun owner a
cui rivolgere un gate di confine. Era l'unico posto in cui tutta la macchina
per-scope costruita il 7 agosto degradava **in silenzio**.

Il caso d'uso è coperto dai DM, che sono topic e quindi scope veri. La `default`
è anteriore ai DM ed era rimasta.

**Un errore di misura, registrato perché è ripetibile.** La prima misura diceva
che quella chat non esisteva: `docker exec … python -c "manager._chats"` avvia un
processo NUOVO e ispeziona un'altra istanza del modulo, non il server vivo. La
risposta vera è arrivata da `/clodia/runtime/sessions`, cioè chiedendo al
processo che gira. Stessa famiglia dell'errore del 6 agosto sui mount letti
senza le sorgenti.
"""
from __future__ import annotations

import inspect
import unittest

from . import main as M
from .sdk_runtime import session as S


class RetirementTests(unittest.TestCase):
    def test_nothing_is_created_at_boot(self):
        self.assertFalse(hasattr(M, "_safe_create_default"))

    def test_the_boot_path_does_not_mention_it(self):
        src = inspect.getsource(M)
        self.assertNotIn("_safe_create_default", src)

    def test_the_reaper_protects_nobody_any_more(self):
        """La protezione esisteva solo per quella chat: tenerla senza soggetto
        significherebbe uno spawn immortale scelto da nessuno."""
        src = inspect.getsource(M)
        self.assertNotIn("protect={", src)

    def test_the_name_survives_with_the_reason(self):
        """Il nome compare nei log e negli storici: chi lo incontra deve trovare
        la nota, non un'assenza."""
        self.assertEqual(S.RETIRED_DEFAULT_CHAT_ID, "default")
        self.assertIn("RITIRATA", inspect.getsource(S)[:8000])

    def test_the_old_name_is_gone(self):
        """Se restasse, un import lo rimetterebbe in uso senza accorgersene."""
        self.assertFalse(hasattr(S, "DEFAULT_CHAT_ID"))


if __name__ == "__main__":
    unittest.main()
