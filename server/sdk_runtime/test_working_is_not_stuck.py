"""Un turno che lavora in silenzio non è un turno piantato.

agents-notebook A13. `api/agents.py:_live_status` chiama `blocked` una sessione
in `thinking` la cui `last_activity` è più vecchia di `_STUCK_AFTER_S` (180s).
Ma `last_activity` si muoveva solo in `_record`, cioè quando un MESSAGGIO veniva
scritto nel file di sessione: un turno che passa dieci minuti in tool call non
aggiornava nulla e veniva dichiarato fermo mentre lavorava.

Misurato il 16 ago 2026: `fullstack-dev#1` riportato `blocked` durante un turno
di undici minuti concluso normalmente. La quantità misurata era «non ha
parlato», presentata come «non sta lavorando».

Il segnale conta perché è l'unico che distingue *sta lavorando* da *è appeso*, e
sbagliava proprio sui turni lunghi — cioè quando serve. Con A13 la stessa
informazione comparirà su una riga per ogni istanza di un super-nodo: un segnale
sbagliato una volta diventerebbe sbagliato quattro.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone


class LastActivityMovesOnProgressTests(unittest.TestCase):
    def test_the_claude_loop_touches_last_activity_next_to_the_watchdog(self) -> None:
        """Il progresso è già misurato per il watchdog (`_last_event_at`): la
        correzione è aggiornare lì anche ciò che l'API legge, senza introdurre
        una seconda nozione di «vivo» che potrebbe divergere dalla prima."""
        import inspect
        from . import session as s
        src = inspect.getsource(s.ChatSession._collect_response)
        self.assertIn("_last_event_at", src)
        self.assertIn("self.last_activity = datetime.now(timezone.utc)", src,
                      "il ciclo eventi non aggiorna last_activity: un turno "
                      "silenzioso tornerà a sembrare piantato")

    def test_every_runtime_touches_it_not_just_claude(self) -> None:
        """codex e opencode non passano dal loop dell'SDK claude, e hanno due
        percorsi DIVERSI fra loro: `_handle_event` per codex, `_handle_parts` per
        opencode. Coprirne uno solo lascerebbe metà della colonia a sembrare
        piantata — messaggero, segretario e minerva girano su opencode."""
        import inspect
        from . import session as s
        for cls, metodo in ((s.CodexChatSession, "_handle_event"),
                            (s.OpenCodeChatSession, "_handle_parts")):
            with self.subTest(runtime=cls.__name__):
                src = inspect.getsource(getattr(cls, metodo))
                self.assertIn("self.last_activity = datetime.now(timezone.utc)", src)


class StuckIsStillDetectedTests(unittest.TestCase):
    """La correzione non deve spegnere il rilevamento: `blocked` resta.

    Se `last_activity` si muovesse per conto suo (un heartbeat, un timer) il
    segnale direbbe sempre «vivo» e sarebbe inutile nell'altro verso — che è il
    difetto opposto e altrettanto silenzioso.
    """

    def test_silence_beyond_the_threshold_is_still_blocked(self) -> None:
        from ..api import agents as a
        vecchio = (datetime.now(timezone.utc)
                   - timedelta(seconds=a._STUCK_AFTER_S + 60)).isoformat()
        self.assertEqual("blocked", a._live_status("thinking", vecchio))

    def test_recent_progress_is_running(self) -> None:
        from ..api import agents as a
        adesso = datetime.now(timezone.utc).isoformat()
        self.assertEqual("running", a._live_status("thinking", adesso))

    def test_idle_and_stopped_are_untouched(self) -> None:
        from ..api import agents as a
        adesso = datetime.now(timezone.utc).isoformat()
        self.assertEqual("idle", a._live_status("idle", adesso))
        self.assertEqual("stopped", a._live_status("stopped", adesso))


if __name__ == "__main__":
    unittest.main()
