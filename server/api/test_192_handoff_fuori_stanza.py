"""clodia-platform#192 · l'handoff verso chi non è nella stanza non può essere muto.

La terza domanda aperta di #192 — «e se il bot raccomandato non ce la fa?» — era
decisa a metà. Lo **stop dopo un handoff** c'è ed è giusto; quello che mancava è
che si vedesse. Oggi un `@X` verso un non-partecipante attraversa
`_maybe_delegate` e finisce nel `return` muto dopo il filtro `plan`:

    --- handoff a NON partecipante (@accountant fuori stanza)
    turni avviati : 0
    messaggi posti: NESSUNO

Zero messaggi e nemmeno una riga di log: per chi guarda il canale il
coordinatore ha detto «questa è di @accountant» e poi non è successo più niente,
che è indistinguibile da un guasto.

Colpisce proprio il coordinatore perché è l'unico che arriva lì *sapendo* che
nella stanza non c'è nessuno di pertinente, con in mano l'output di
`topic.suggest_team` — una lista di nomi della COLONIA, non del canale. Prendere
un nome da quella lista e scriverlo con `@` invece che dentro `<!-- invite= -->`
è il passo falso più probabile, e l'esito è il silenzio.

Stessa famiglia dei rami muti già chiusi (#332, #360, #367), su un ramo rimasto
fuori.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a


class _Base(unittest.IsolatedAsyncioTestCase):
    """La stanza ha `clodia` (coordinatore) e `owner`. `accountant` esiste in
    colonia ma NON è partecipante: è il caso della issue."""

    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:00Z"),
            "fullstack-dev": _a("fullstack-dev", "normal", "P1", "2026-02-01T00:00:00Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig_get = channels.registry.get_by_name
        self._orig_track = channels._track_routing_decision
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        channels._track_routing_decision = lambda _p: None
        os.environ.pop("CHANNEL_MULTI_RESPONDER", None)
        os.environ.pop("CLODIA_MAX_DELEGATION_HOPS", None)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._track_routing_decision = self._orig_track

    async def _delegate(self, text, participants=("owner", "clodia"), hop=1):
        posts: list[dict] = []
        eventi: list = []

        def post(_t, _n, author, txt, kind="human", **_k):
            row = {"id": str(len(posts) + 1), "author": author, "text": txt, "kind": kind}
            posts.append(row)
            return row

        async def publish(ev):
            eventi.append(ev)

        start = AsyncMock(return_value=True)
        with contextlib.ExitStack() as st:
            for cm in (
                patch.object(channels, "_start_turn", start),
                patch.object(channels, "_provider_seal_ok", return_value=True),
                patch.object(channels.topics_client, "open_topic", return_value={
                    "meta": {"tier": "P0", "participants": list(participants)}}),
                patch.object(channels.topics_client, "post_message", side_effect=post),
                patch.object(channels.topics_client, "list_messages", return_value=[]),
                patch.object(channels, "_channel_message", AsyncMock()),
                patch.object(channels.bus, "publish", side_effect=publish),
            ):
                st.enter_context(cm)
            await channels._maybe_delegate("P0", "ops", "clodia", text, "owner", hop)
        return posts, start, eventi


class AHandoffOutOfTheRoomIsAnnouncedTests(_Base):
    async def test_the_channel_is_told_and_the_agent_is_named(self) -> None:
        """Il difetto misurato: oggi qui non si posta niente."""
        posts, start, _ev = await self._delegate(
            "Questa è contabilità: @accountant puoi prenderla tu?")

        start.assert_not_awaited()
        sistema = [p for p in posts if p["kind"] == "system"]
        self.assertEqual(1, len(sistema), "l'handoff fuori stanza è ancora muto")
        self.assertEqual(channels._ROUTING_DIALOG_AUTHOR, sistema[0]["author"])
        self.assertIn("accountant", sistema[0]["text"])
        self.assertIn("clodia", sistema[0]["text"],
                      "la nota deve dire anche CHI ha taggato")

    async def test_the_remedy_is_clickable_when_the_name_exists(self) -> None:
        posts, _start, _ev = await self._delegate("@accountant prendi tu")
        self.assertIn("<!-- invite=accountant -->", posts[-1]["text"])

    async def test_a_name_that_does_not_exist_is_not_a_target(self) -> None:
        """Il confine contro il rumore, e la sola deviazione dal piano.

        Il piano chiedeva la nota anche per un nome sconosciuto, solo senza
        pill. Ma `@nessuno` e i refusi non sono bersagli — il limite catena tace
        già su di essi di proposito (`test_delegation_limit_visible`), e una
        nota «invitalo» su un nome che non esiste mette un vicolo cieco al posto
        dell'altro. Il registro è il filtro; il refuso resta nel log.
        """
        posts, _start, eventi = await self._delegate("@commercialista prendi tu")
        self.assertEqual([], posts)
        self.assertEqual([], eventi)

    async def test_two_names_out_of_the_room_are_listed_together(self) -> None:
        """Due nomi fuori stanza sono un elenco, non una scelta: «@a o @b sono
        stati taggati» non è la frase che una persona deve leggere, e la pill le
        propone entrambe perché servono entrambe."""
        posts, _start, _ev = await self._delegate(
            "Serve la coppia: @accountant e @fullstack-dev.")
        testo = posts[-1]["text"]
        self.assertIn("@accountant e @fullstack-dev", testo)
        self.assertIn("<!-- invite=accountant,fullstack-dev -->", testo)

    async def test_a_participant_still_starts_and_nothing_is_announced(self) -> None:
        """Controprova: il successo non si annuncia."""
        posts, start, _ev = await self._delegate(
            "@fullstack-dev procedi",
            participants=("owner", "clodia", "fullstack-dev"))
        self.assertEqual(1, start.await_count)
        self.assertEqual("fullstack-dev", start.await_args.args[3].name)
        self.assertEqual([], [p for p in posts
                              if p["author"] == channels._ROUTING_DIALOG_AUTHOR])

    async def test_a_reply_without_tags_says_nothing(self) -> None:
        """La guardia contro il rumore: verde oggi, ed è lì per restarci. Se la
        nota si emettesse su ogni reply, il canale si riempirebbe di avvisi su
        menzioni che non c'erano."""
        posts, start, _ev = await self._delegate("Fatto, nessuno da chiamare.")
        start.assert_not_awaited()
        self.assertEqual([], posts)

    async def test_a_citation_out_of_the_room_says_nothing(self) -> None:
        """`$nome` non è una convocazione (R12): non c'è nessun turno negato da
        dichiarare, dentro o fuori dalla stanza."""
        posts, start, _ev = await self._delegate("Per conoscenza $accountant")
        start.assert_not_awaited()
        self.assertEqual([], posts)

    async def test_the_bus_hears_it_too(self) -> None:
        """Chi non guarda la chat deve poterlo sapere, come per il limite catena."""
        _posts, _start, eventi = await self._delegate("@accountant prendi tu")
        fuori = [e for e in eventi
                 if getattr(e, "type", None) == "routing_decision"
                 and e.payload.get("mode") == "delega-fuori-stanza"]
        self.assertEqual(1, len(fuori))
        self.assertEqual(["accountant"], fuori[0].payload["negati"])
        self.assertEqual("clodia", fuori[0].payload["from_agent"])


if __name__ == "__main__":
    unittest.main()
