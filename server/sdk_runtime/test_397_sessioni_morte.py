"""Una sessione col subprocess morto non aspetta il turno successivo (#397 §2).

Il 26 set 2026 il CLI di `clodia-290` è terminato da solo alle 16:33 (exit
143). Nessuno se n'è accorto: alle 16:44 il giro schedulato ci ha scritto
sopra, il turno è caduto ed è stato quel fallimento a innescare la recovery.
Undici minuti in cui la sessione risultava pronta e non lo era — e nel
frattempo lo spawn su disco e l'uid di sandbox restavano allocati a un
processo che non esisteva più.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from . import session as sess
from .session import ChatManager, subprocess_morto


class _Proc:
    def __init__(self, returncode=None):
        self.returncode = returncode


class _FintaSessione:
    """Il minimo che `_evict` tocca: stop(), to_dict(), last_activity."""

    def __init__(self, chat_id, *, client=None, proc=None, in_turno=False,
                 idle_da=0.0):
        self.chat_id = chat_id
        self._client = client
        if proc is not None:
            self._proc = proc
        self.last_activity = (datetime.now(timezone.utc)
                              - timedelta(seconds=idle_da))
        self.stopped = False
        self._current_turn_task = None
        if in_turno:
            self._current_turn_task = SimpleNamespace(done=lambda: False)

    async def stop(self):
        self.stopped = True

    def to_dict(self):
        return {"chat_id": self.chat_id}


def _client_con_processo(returncode):
    return SimpleNamespace(_transport=SimpleNamespace(_process=_Proc(returncode)))


class ProbeTest(IsolatedAsyncioTestCase):
    async def test_processo_terminato_e_morto(self):
        self.assertIs(True, subprocess_morto(
            _FintaSessione("c", client=_client_con_processo(143))))

    async def test_processo_vivo_non_e_morto(self):
        self.assertIs(False, subprocess_morto(
            _FintaSessione("c", client=_client_con_processo(None))))

    async def test_exit_zero_conta_come_morto(self):
        # Un processo uscito bene è comunque un processo che non c'è più: la
        # sessione che lo teneva è un guscio esattamente come con il 143.
        self.assertIs(True, subprocess_morto(
            _FintaSessione("c", client=_client_con_processo(0))))

    async def test_senza_client_non_si_sa(self):
        # Sessione mai avviata, ferma, o in mezzo a una recovery: rispondere
        # «morta» qui significherebbe evincere mentre qualcuno la ricrea.
        self.assertIsNone(subprocess_morto(_FintaSessione("c")))

    async def test_runtime_che_non_espone_il_processo_non_si_sa(self):
        self.assertIsNone(subprocess_morto(
            _FintaSessione("c", client=SimpleNamespace())))
        self.assertIsNone(subprocess_morto(
            _FintaSessione("c", client=SimpleNamespace(_transport=SimpleNamespace()))))

    async def test_proc_esplicito_di_codex_opencode(self):
        self.assertIs(True, subprocess_morto(_FintaSessione("c", proc=_Proc(1))))
        self.assertIs(False, subprocess_morto(_FintaSessione("c", proc=_Proc(None))))

    async def test_codex_fuori_turno_non_e_una_morte(self):
        # Codex apre un processo per turno e lo azzera nel `finally`: fuori dal
        # turno `_proc` è None, e scambiarlo per una morte evincerebbe a ogni
        # tick ogni sessione codex ferma.
        finta = _FintaSessione("c")
        finta._proc = None
        self.assertIsNone(subprocess_morto(finta))


class ReapDeadTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.pubblicati = []

        async def _publish(evento):
            self.pubblicati.append(evento)

        self._orig = sess.bus.publish
        sess.bus.publish = _publish
        self.addCleanup(lambda: setattr(sess.bus, "publish", self._orig))
        self.manager = ChatManager()

    def _aggiungi(self, chat):
        self.manager._chats[chat.chat_id] = chat
        return chat

    async def test_evince_la_sessione_morta_e_lascia_la_viva(self):
        morta = self._aggiungi(_FintaSessione("morta", client=_client_con_processo(143)))
        viva = self._aggiungi(_FintaSessione("viva", client=_client_con_processo(None)))

        evinte = await self.manager.reap_dead()

        self.assertEqual(["morta"], evinte)
        self.assertTrue(morta.stopped)
        self.assertFalse(viva.stopped)
        self.assertNotIn("morta", self.manager._chats)
        self.assertIn("viva", self.manager._chats)
        self.assertEqual(["chat_updated"], [e.type for e in self.pubblicati])

    async def test_non_tocca_una_sessione_in_mezzo_a_un_turno(self):
        # Il processo può risultare morto proprio mentre il turno sta cadendo:
        # portarla via da sotto i piedi al chiamante è peggio del guasto.
        dentro = self._aggiungi(_FintaSessione(
            "in-turno", client=_client_con_processo(143), in_turno=True))
        self.assertEqual([], await self.manager.reap_dead())
        self.assertFalse(dentro.stopped)
        self.assertIn("in-turno", self.manager._chats)

    async def test_non_tocca_chi_non_sa_di_essere_morto(self):
        ignota = self._aggiungi(_FintaSessione("ignota"))
        self.assertEqual([], await self.manager.reap_dead())
        self.assertFalse(ignota.stopped)

    async def test_protect_ha_la_precedenza(self):
        protetta = self._aggiungi(_FintaSessione(
            "protetta", client=_client_con_processo(143)))
        self.assertEqual([], await self.manager.reap_dead(protect=["protetta"]))
        self.assertFalse(protetta.stopped)

    async def test_uno_stop_che_esplode_non_ferma_gli_altri(self):
        class _Esplosiva(_FintaSessione):
            async def stop(self):
                raise RuntimeError("stop fallito")

        self._aggiungi(_Esplosiva("rotta", client=_client_con_processo(143)))
        buona = self._aggiungi(_FintaSessione("buona", client=_client_con_processo(143)))

        evinte = await self.manager.reap_dead()

        self.assertEqual(["buona"], evinte)
        self.assertTrue(buona.stopped)

    async def test_reap_idle_continua_a_funzionare(self):
        # `reap_idle` e `reap_dead` condividono ora la stessa eviction: se la
        # fattorizzazione avesse perso un pezzo, si vedrebbe qui.
        vecchia = self._aggiungi(_FintaSessione(
            "vecchia", client=_client_con_processo(None), idle_da=4000))
        recente = self._aggiungi(_FintaSessione(
            "recente", client=_client_con_processo(None), idle_da=1))

        evinte = await self.manager.reap_idle(1800)

        self.assertEqual(["vecchia"], evinte)
        self.assertTrue(vecchia.stopped)
        self.assertFalse(recente.stopped)
        self.assertNotIn("vecchia", self.manager._chats)

    async def test_reap_idle_non_tocca_un_turno_in_corso(self):
        dentro = self._aggiungi(_FintaSessione(
            "in-turno", client=_client_con_processo(None), in_turno=True,
            idle_da=9999))
        self.assertEqual([], await self.manager.reap_idle(1800))
        self.assertFalse(dentro.stopped)


class TickTest(IsolatedAsyncioTestCase):
    def test_il_tick_periodico_chiama_reap_dead(self):
        # Una probe che nessuno interroga non esiste: il difetto di #397 §2 è
        # proprio che il dato c'era e nessuno lo guardava.
        import inspect

        from .. import main
        sorgente = inspect.getsource(main._lifespan)
        self.assertIn("reap_dead()", sorgente)
        # e non solo quando l'idle reaper è acceso
        posizione_dead = sorgente.index("reap_dead()")
        posizione_idle = sorgente.index("reap_idle(ttl)")
        self.assertLess(posizione_dead, posizione_idle)


def _run(coro):  # pragma: no cover - utilità
    return asyncio.get_event_loop().run_until_complete(coro)
