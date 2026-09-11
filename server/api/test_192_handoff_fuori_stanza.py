"""clodia-platform#192 · l'handoff verso chi non è nella stanza non è più muto.

Il terzo punto aperto della issue — «e se il bot raccomandato non può servire la
richiesta?» — ha una risposta decisa (stop dopo UN handoff, il canale torna
all'umano) e tre strade per arrivarci, di cui solo due parlano:

| il bersaglio del `@`…                  | si vede?                        |
|----------------------------------------|---------------------------------|
| partecipa e il suo turno crasha        | sì, `_announce_failure`         |
| partecipa e rifiuta l'attivazione      | sì, la guardia di `_start_turn` |
| **non partecipa al canale**            | **no: silenzio totale**         |

Misurato su `_maybe_delegate` con `@accountant` fuori dalla stanza: zero turni
avviati e ZERO messaggi. Il filtro `_seed_name(t) in participants` scarta il tag
e l'uscita `if not plan: return` non lascia nemmeno una riga di log.

Colpisce soprattutto il coordinatore, che è l'unico ad arrivare lì *sapendo* che
nella stanza non c'è nessuno di pertinente e con in mano l'output di
`topic.suggest_team` — una lista di nomi della COLONIA, non della stanza.
Prendere un nome da quella lista e scriverlo con `@` invece che dentro
`<!-- invite= -->` è la mossa sbagliata più probabile, e l'esito è il peggiore:
la persona legge «questa è di @X» e poi non succede più niente.

Il rimedio non è un secondo ripiego — una seconda cascata rimetterebbe il router
a decidere dopo che un modello ha già deciso. È una riga che dice cosa è
successo, con la pill che rende il rimedio CLICCABILE (lezione di #332: «serve un
messaggio con una sola menzione» era vero e inerte).
"""
from __future__ import annotations

import contextlib
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_r3_one_mention import _Base


class _FuoriStanza(_Base):
    """Stanza a tre: owner, il coordinatore e un worker. `accountant` esiste nel
    registro della colonia ma NON è partecipante — è il caso della issue."""

    STANZA = ["owner", "clodia", "worker"]

    async def _delega(self, testo: str, *, participants=None):
        start = AsyncMock(return_value=True)
        publish = AsyncMock()
        posts, apri = self._channel(participants or self.STANZA)
        with apri(patch.object(channels, "_start_turn", start),
                  patch.object(channels.bus, "publish", publish)):
            await channels._maybe_delegate(
                "P0", "ops", "clodia", testo, "owner", 0)
        return posts, start, publish


class TheSilentBranchSpeaksTests(_FuoriStanza):

    async def test_a_handoff_outside_the_room_leaves_a_note(self) -> None:
        """Il caso misurato: oggi zero messaggi, e per chi guarda il canale è
        indistinguibile da un agente che non risponde."""
        posts, start, _publish = await self._delega(
            "Questa è competenza di @accountant, passo a lui.")

        start.assert_not_awaited()
        self.assertEqual(
            1, len(posts),
            "handoff verso un non partecipante servito in silenzio: il canale "
            "non dice che nessun turno è partito")
        nota = posts[-1]
        self.assertEqual(channels._ROUTING_DIALOG_AUTHOR, nota["author"])
        self.assertEqual("system", nota["kind"])
        self.assertIn("accountant", nota["text"])
        self.assertIn("clodia", nota["text"])

    async def test_the_note_offers_the_invite_when_the_name_is_registered(self) -> None:
        """Il rimedio dev'essere cliccabile: `<!-- invite=X -->` è la proposta
        che l'owner esegue col bottone, la stessa che usa il mandato del seed."""
        posts, _start, _publish = await self._delega("Serve @accountant.")

        self.assertIn("<!-- invite=accountant -->", posts[-1]["text"])

    async def test_an_unknown_name_gets_no_note_at_all(self) -> None:
        """Un nome che non esiste nella colonia non ha un rimedio da offrire —
        invitarlo non si può — e una nota su di lui sarebbe rumore su una
        menzione che non c'era. È lo stesso confine che
        `test_delegation_limit_visible` fissa per il limite catena con
        `@nessuno`: resta la riga di log, non il messaggio."""
        posts, _start, publish = await self._delega("Ci pensa @pippo.")

        self.assertEqual([], posts)
        publish.assert_not_awaited()

    async def test_the_monitor_hears_it_too(self) -> None:
        """Come per il limite catena (#332): chi non sta guardando la chat deve
        poter sapere che una menzione è morta lì."""
        _posts, _start, publish = await self._delega("Tocca a @accountant.")

        eventi = [c.args[0] for c in publish.await_args_list]
        fuori = [e for e in eventi
                 if (e.payload or {}).get("mode") == "delega-fuori-stanza"]
        self.assertEqual(1, len(fuori), f"nessun evento fuori-stanza fra {eventi}")
        payload = fuori[0].payload
        self.assertEqual(["accountant"], payload["negati"])
        self.assertEqual("clodia", payload["from_agent"])


class TheNoteDoesNotBecomeNoiseTests(_FuoriStanza):
    """Il confine del §3 del piano: la nota esiste per un ramo muto, non per
    commentare ogni reply. Questi due sono verdi oggi e devono restarci."""

    async def test_a_served_handoff_says_nothing(self) -> None:
        """Controprova: il bersaglio è nella stanza, il turno parte, e un
        successo non si annuncia."""
        posts, start, publish = await self._delega("@worker fallo tu")

        self.assertEqual(1, start.await_count)
        self.assertEqual("worker", start.await_args.args[3].name)
        self.assertEqual(
            [], [p for p in posts if p["author"] == channels._ROUTING_DIALOG_AUTHOR])

    async def test_a_reply_without_tags_says_nothing(self) -> None:
        posts, start, publish = await self._delega("Fatto, non serve nessuno.")

        start.assert_not_awaited()
        self.assertEqual([], posts)
        publish.assert_not_awaited()

    async def test_a_self_tag_alone_is_not_a_handoff(self) -> None:
        """`@clodia` scritto da clodia è scartato dal filtro di sempre: non è un
        bersaglio fuori stanza, ed è già dentro. Zero note."""
        posts, start, _publish = await self._delega("Me ne occupo io, @clodia.")

        start.assert_not_awaited()
        self.assertEqual([], posts)


class TheWatcherKeepsItsOwnDoorTests(_FuoriStanza):
    """In modalità debug `@sysadmin` fuori stanza SVEGLIA il guardiano: quel
    ramo un turno lo apre, e dirgli «nessun turno è partito» sarebbe falso."""

    async def test_the_debug_call_to_the_watcher_is_not_reported_as_dead(self) -> None:
        self.agents[channels.debug_watch.WATCHER] = channels.registry.get_by_name(
            "worker").model_copy(update={"name": channels.debug_watch.WATCHER})
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(self.STANZA)
        with apri(patch.object(channels, "_start_turn", start),
                  patch.object(channels.debug_watch, "enabled", lambda: True),
                  patch.object(channels, "_spawn_bg", lambda _coro: _coro.close()),
                  patch.object(channels.bus, "publish", AsyncMock())):
            await channels._maybe_delegate(
                "P0", "ops", "clodia",
                f"@{channels.debug_watch.WATCHER} sono bloccato", "owner", 0)

        self.assertEqual([], posts)


if __name__ == "__main__":
    unittest.main()
