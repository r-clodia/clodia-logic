"""A chi va una menzione, e chi aspetta.

Regola di Davide, 18 ago 2026, verbatim:

    «se io menziono A e nessuno spawn di A è in esecuzione allora ne viene creato
    uno. Se invece è in esecuzione allora la menzione ne spawna uno nuovo fino al
    raggiungimento del limite di multispawn. Quando il limite è raggiunto la
    menzione arriva al primo spawn che finisce il suo turno. Per seed
    non-multispawn il limite=1»

Tre divergenze rispetto all'implementazione, di cui la seconda costava tempo
misurabile:

1. il limite non era espresso per i seed non-multispawn: `_resolve_ordinal` non
   veniva chiamata e la sessione unica accodava da sé. Stesso effetto, due posti;
2. al limite si scegliva l'ordinale MINIMO — cioè si decideva l'attesa in
   anticipo. Con `#1` dentro un turno da dieci minuti e `#3` che si libera in
   cinque secondi, la menzione aspettava dieci minuti;
3. `@nome#N` steerava l'allocazione ed era clampato in silenzio: chi leggeva
   `clodia-124` e scriveva `@clodia#124` risvegliava l'istanza 4.

Sull'attesa c'è una proprietà che è facile perdere riscrivendo: OGGI due menzioni
a un agente occupato si accodano sul lock della sessione, che è FIFO, quindi sono
servite nell'ordine in cui sono state scritte. Un'attesa a sondaggio senza coda
farebbe correre i waiter l'uno contro l'altro, e la seconda domanda potrebbe
ricevere risposta prima della prima. Per questo l'attesa passa da un lock per
(canale, seed): serve all'equità, non alla mutua esclusione.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import channels as ch


def _spec(nome="fullstack-dev", multi=True, cap=4):
    return SimpleNamespace(name=nome, multi_spawn=multi, max_spawns=cap)


class _Chat:
    def __init__(self, chat_id, busy=False, spawn=None):
        self.chat_id = chat_id
        self._lock = asyncio.Lock()
        self._busy = busy
        # forma del runtime Claude: la dir sta in `_spawn`
        self._spawn = SimpleNamespace(dir=SimpleNamespace(name=spawn)) if spawn else None

    def libera(self):
        self._busy = False


class _Manager:
    """Manager finto: `locked()` è pilotato dal flag, non da un lock vero, così
    un test non deve tenere davvero un turno in corso."""

    def __init__(self, chats):
        self._c = {c.chat_id: c for c in chats}

    def list(self):
        return list(self._c.values())

    def get(self, cid):
        return self._c[cid]


def _fake_lock_state(chats):
    """Fa sì che `_sessions_of` legga il flag `_busy` di questi fake."""
    for c in chats:
        c._lock = SimpleNamespace(locked=lambda c=c: c._busy)


class CapTests(unittest.TestCase):
    def test_a_non_multi_spawn_seed_has_cap_one(self) -> None:
        self.assertEqual(1, ch._spawn_cap(_spec(multi=False)))

    def test_a_multi_spawn_seed_has_its_declared_cap(self) -> None:
        self.assertEqual(3, ch._spawn_cap(_spec(cap=3)))

    def test_zero_means_undeclared_and_a_negative_means_one(self) -> None:
        """Semantica preesistente, conservata e ora scritta: `0`/`None` valgono
        «non dichiarato» → il default 4 (`0 or 4`), mentre un valore negativo è
        riportato a 1 dal `max(1, …)`. Va detto in un test perché `max_spawns: 0`
        che significa «quattro» è la cosa che chi legge il seed non indovina."""
        for valore, atteso in ((0, 4), (None, 4), (-3, 1)):
            with self.subTest(max_spawns=valore):
                self.assertEqual(atteso, ch._spawn_cap(SimpleNamespace(
                    name="x", multi_spawn=True, max_spawns=valore)))


class SessionsOfTests(unittest.TestCase):
    """Riconosce ENTRAMBE le forme di chiave. La chiave senza `#N` non è stata
    unificata di proposito: aggiungerlo orfanerebbe la sessione e il suo file di
    storia su ogni istanza già in esercizio."""

    def test_it_sees_the_unsuffixed_key_too(self) -> None:
        chats = [_Chat("chan:SEAL-1:ch:clodia", busy=True)]
        _fake_lock_state(chats)
        with patch.object(ch, "manager", _Manager(chats)):
            self.assertEqual([("chan:SEAL-1:ch:clodia", True)],
                             ch._sessions_of("SEAL-1", "ch", "clodia"))

    def test_it_sees_the_suffixed_keys(self) -> None:
        chats = [_Chat("chan:SEAL-1:ch:clodia#1", busy=False),
                 _Chat("chan:SEAL-1:ch:clodia#2", busy=True)]
        _fake_lock_state(chats)
        with patch.object(ch, "manager", _Manager(chats)):
            self.assertEqual(
                [("chan:SEAL-1:ch:clodia#1", False), ("chan:SEAL-1:ch:clodia#2", True)],
                ch._sessions_of("SEAL-1", "ch", "clodia"))

    def test_another_seed_with_the_same_prefix_is_not_confused(self) -> None:
        """`clodia` non deve raccogliere le sessioni di `clodia-primal`."""
        chats = [_Chat("chan:SEAL-1:ch:clodia-primal", busy=True)]
        _fake_lock_state(chats)
        with patch.object(ch, "manager", _Manager(chats)):
            self.assertEqual([], ch._sessions_of("SEAL-1", "ch", "clodia"))

    def test_another_channel_is_not_ours(self) -> None:
        chats = [_Chat("chan:SEAL-1:altro:clodia#1", busy=False)]
        _fake_lock_state(chats)
        with patch.object(ch, "manager", _Manager(chats)):
            self.assertEqual([], ch._sessions_of("SEAL-1", "ch", "clodia"))


class AddressingASpawnByNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chats = [_Chat("chan:SEAL-1:ch:clodia#1", spawn="clodia-124"),
                      _Chat("chan:SEAL-1:ch:clodia#2", spawn="clodia-125")]
        _fake_lock_state(self.chats)
        p = patch.object(ch, "manager", _Manager(self.chats))
        p.start()
        self.addCleanup(p.stop)

    def test_the_name_read_in_chat_is_the_name_you_write(self) -> None:
        self.assertEqual("chan:SEAL-1:ch:clodia#2",
                         ch._chat_of_spawn("SEAL-1", "ch", "clodia", "clodia-125"))

    def test_a_dead_spawn_is_not_addressable(self) -> None:
        """Non si materializza niente: l'allocazione normale serve comunque la
        menzione, e il ripiego finisce nel log invece di essere muto."""
        self.assertIsNone(ch._chat_of_spawn("SEAL-1", "ch", "clodia", "clodia-999"))

    def test_no_name_asked_no_lookup(self) -> None:
        self.assertIsNone(ch._chat_of_spawn("SEAL-1", "ch", "clodia", None))


class FirstToFinishTests(unittest.IsolatedAsyncioTestCase):
    """«la menzione arriva al primo spawn che finisce il suo turno»."""

    async def test_it_waits_and_takes_whoever_frees_first(self) -> None:
        chats = [_Chat("chan:SEAL-1:ch:fullstack-dev#1", busy=True),
                 _Chat("chan:SEAL-1:ch:fullstack-dev#2", busy=True)]
        _fake_lock_state(chats)

        async def libera_il_secondo():
            await asyncio.sleep(0.05)
            chats[1].libera()          # il #2 finisce prima del #1

        with patch.object(ch, "manager", _Manager(chats)), \
                patch.object(ch, "_FREE_POLL_SEC", 0.01):
            ch._claimed.clear()
            _, cid = await asyncio.gather(
                libera_il_secondo(),
                ch._await_free_session("SEAL-1", "ch", _spec(cap=2)))
        self.assertEqual("chan:SEAL-1:ch:fullstack-dev#2", cid,
                         "la menzione non è andata al primo che ha finito")
        ch._claimed.clear()

    async def test_the_chosen_session_is_claimed_so_two_waiters_do_not_collide(self) -> None:
        """Fra la scelta e il momento in cui il turno prende il lock c'è una
        finestra in cui la sessione risulta ancora libera: senza prenotazione due
        waiter la assegnerebbero a sé, e uno dei due turni finirebbe in coda su
        una sessione che credeva libera."""
        chats = [_Chat("chan:SEAL-1:ch:fullstack-dev#1", busy=False)]
        _fake_lock_state(chats)
        with patch.object(ch, "manager", _Manager(chats)), \
                patch.object(ch, "_FREE_POLL_SEC", 0.01):
            ch._claimed.clear()
            primo = await ch._await_free_session("SEAL-1", "ch", _spec(cap=1))
            self.assertEqual("chan:SEAL-1:ch:fullstack-dev#1", primo)
            self.assertIn(primo, ch._claimed)
            secondo = await ch._await_free_session(
                "SEAL-1", "ch", _spec(cap=1), timeout=0.05)
            self.assertIsNone(secondo, "la stessa sessione è stata data due volte")
        ch._claimed.clear()

    async def test_waiting_mentions_are_served_in_arrival_order(self) -> None:
        """L'equità che il lock di sessione dava e che un sondaggio nudo
        perderebbe: la seconda domanda non deve essere servita prima della prima."""
        chats = [_Chat("chan:SEAL-1:ch:fullstack-dev#1", busy=True)]
        _fake_lock_state(chats)
        ordine: list[str] = []

        async def menzione(etichetta, ritardo):
            await asyncio.sleep(ritardo)          # arrivi distinti e ordinati
            cid = await ch._await_free_session(
                "SEAL-1", "ch", _spec(cap=1), timeout=2.0)
            ordine.append(etichetta)
            ch._claimed.discard(cid)              # il "turno" finisce subito
            chats[0]._busy = False

        async def libera():
            await asyncio.sleep(0.08)
            chats[0].libera()

        with patch.object(ch, "manager", _Manager(chats)), \
                patch.object(ch, "_FREE_POLL_SEC", 0.01):
            ch._claimed.clear()
            ch._wait_locks.clear()
            await asyncio.gather(menzione("prima", 0.0),
                                 menzione("seconda", 0.02),
                                 menzione("terza", 0.04),
                                 libera())
        self.assertEqual(["prima", "seconda", "terza"], ordine)
        ch._claimed.clear()
        ch._wait_locks.clear()

    async def test_a_hopeless_wait_gives_up_loudly(self) -> None:
        """Se nessuno si libera, la menzione non parte — e lo dice. Aspettare per
        sempre sarebbe un turno che nessuno vede mai e nessuno spiega."""
        chats = [_Chat("chan:SEAL-1:ch:fullstack-dev#1", busy=True)]
        _fake_lock_state(chats)
        with patch.object(ch, "manager", _Manager(chats)), \
                patch.object(ch, "_FREE_POLL_SEC", 0.01):
            ch._claimed.clear()
            with self.assertLogs("agent-server.api.channels", level="WARNING") as log:
                cid = await ch._await_free_session(
                    "SEAL-1", "ch", _spec(cap=1), timeout=0.05)
        self.assertIsNone(cid)
        self.assertIn("libero", "\n".join(log.output))


if __name__ == "__main__":
    unittest.main()


class ResetForgetsEverySpawnTests(unittest.IsolatedAsyncioTestCase):
    """«Reset contesto» deve azzerare TUTTE le istanze del partecipante.

    Domanda di Davide, 18 ago: il reset cancella l'AGENTS.md del topic? No — il
    reset posta un marker e chiude le sessioni, e non tocca alcun file (c'è un
    test a parte nel gateway per la via che invece li rimuove).

    Ma leggendolo è venuto fuori un difetto vicino: `_drop_channel_sessions`
    costruiva la sola chiave nuda `chan:t:n:agent`, mentre le istanze multi-spawn
    vivono su `…#1…#N`. Il reset le lasciava in piedi con la loro memoria, e il
    pulsante diceva di aver resettato: per gli agenti che girano come più istanze
    — cioè quelli su cui una conversazione si accumula davvero — non resettava
    niente.
    """

    async def test_it_deletes_the_suffixed_sessions_too(self) -> None:
        chats = [_Chat("chan:SEAL-1:ch:fullstack-dev#1"),
                 _Chat("chan:SEAL-1:ch:fullstack-dev#2"),
                 _Chat("chan:SEAL-1:ch:clodia")]
        _fake_lock_state(chats)
        cancellate: list[str] = []

        class _M(_Manager):
            async def delete(self, cid):
                cancellate.append(cid)

        with patch.object(ch, "manager", _M(chats)):
            out = await ch._drop_channel_sessions(
                "SEAL-1", "ch", ["fullstack-dev", "clodia"])
        self.assertEqual(
            {"chan:SEAL-1:ch:fullstack-dev#1", "chan:SEAL-1:ch:fullstack-dev#2",
             "chan:SEAL-1:ch:clodia"},
            set(cancellate),
            "il reset ha lasciato in piedi delle istanze")
        self.assertEqual(3, len(out))

    async def test_a_participant_without_sessions_is_not_an_error(self) -> None:
        chats = [_Chat("chan:SEAL-1:ch:clodia")]
        _fake_lock_state(chats)

        class _M(_Manager):
            async def delete(self, cid):
                pass

        with patch.object(ch, "manager", _M(chats)):
            out = await ch._drop_channel_sessions("SEAL-1", "ch", ["nessuno", "clodia"])
        self.assertEqual(["chan:SEAL-1:ch:clodia"], out)
