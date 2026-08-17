"""router-notebook R16: il limite della catena non deve essere invisibile.

    «fullstack quando menzionato a volte parte e a volte no»
                                                — Davide, 17 ago 2026

Misurato sui log di `software-house`: la catena arriva regolarmente a `hop 2`, e
`_MAX_DELEGATION_HOPS` è 2. Nei due punti che innescano la delega il controllo era

    if hop < _MAX_DELEGATION_HOPS:
        await _maybe_delegate(...)

quindi al salto successivo `_maybe_delegate` **non veniva chiamata affatto**:
nessun log, nessun messaggio nel canale, nessuna traccia. Il tag `@fullstack-dev`
scritto da un agente al terzo salto semplicemente non esisteva.

Da qui l'intermittenza, che dal canale è imprevedibile:

    Davide → @clodia            hop 0   parte
    clodia → @fullstack-dev     hop 1   parte
    fullstack-dev → @clodia     hop 2   parte
    clodia → @fullstack-dev     hop 3   NIENTE, e nessuno lo dice

Il limite serve — è il freno ai rimpalli fra agenti. Ciò che non serve è il
silenzio: «l'ho chiamato e non risponde» è indistinguibile da un guasto, e chi
guarda il canale non ha modo di sapere a che punto della catena si trova.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
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
        os.environ.pop("CLODIA_MAX_DELEGATION_HOPS", None)

    async def _delegate(self, text, hop, history=None):
        posts: list[dict] = []

        def post(_t, _n, author, txt, kind="human", **_k):
            row = {"id": str(len(posts) + 1), "author": author, "text": txt, "kind": kind}
            posts.append(row)
            return row

        start = AsyncMock(return_value=True)
        with contextlib.ExitStack() as st:
            for cm in (
                patch.object(channels, "_start_turn", start),
                patch.object(channels, "_provider_seal_ok", return_value=True),
                patch.object(channels.topics_client, "open_topic", return_value={
                    "meta": {"tier": "P0",
                             "participants": ["owner", "clodia", "fullstack-dev"]}}),
                patch.object(channels.topics_client, "post_message", side_effect=post),
                patch.object(channels.topics_client, "list_messages",
                             return_value=list(history or [])),
                patch.object(channels, "_channel_message", AsyncMock()),
            ):
                st.enter_context(cm)
            await channels._maybe_delegate(
                "P0", "ops", "clodia", text, "owner", hop)
        return posts, start


class BeyondTheLimitItSaysSoTests(_Base):
    async def test_within_the_limit_the_delegation_happens(self) -> None:
        posts, start = await self._delegate("Ci pensa @fullstack-dev", hop=0)
        self.assertEqual(1, start.await_count)
        self.assertEqual("fullstack-dev", start.await_args.args[3].name)
        self.assertEqual([], [p for p in posts if p["author"] == "router"])

    async def test_at_the_limit_nothing_starts(self) -> None:
        """Il freno resta: non si trasforma un limite in un limite inesistente."""
        posts, start = await self._delegate(
            "Ci pensa @fullstack-dev", hop=channels._max_delegation_hops())
        start.assert_not_awaited()

    async def test_at_the_limit_the_channel_is_told_and_the_agent_is_named(self) -> None:
        """La correzione. Senza questo messaggio «l'ho chiamato e non risponde» è
        indistinguibile da un guasto — ed è come si presentava."""
        posts, _start = await self._delegate(
            "Ci pensa @fullstack-dev", hop=channels._max_delegation_hops())

        self.assertTrue(posts, "il limite blocca ancora in silenzio")
        avviso = posts[-1]
        self.assertEqual("router", avviso["author"])
        self.assertIn("fullstack-dev", avviso["text"])

    async def test_beyond_the_limit_too(self) -> None:
        posts, start = await self._delegate(
            "@fullstack-dev procedi", hop=channels._max_delegation_hops() + 3)
        start.assert_not_awaited()
        self.assertIn("fullstack-dev", posts[-1]["text"])

    async def test_nothing_is_said_when_there_was_nothing_to_serve(self) -> None:
        """Un messaggio senza tag idonei non produce un avviso: il canale non si
        riempie di note su menzioni che non c'erano. `@nessuno` non era un target
        nemmeno dentro il limite."""
        posts, start = await self._delegate(
            "Fatto, nessuno da chiamare. @nessuno",
            hop=channels._max_delegation_hops())
        start.assert_not_awaited()
        self.assertEqual([], posts)

    async def test_a_citation_alone_does_not_produce_a_warning(self) -> None:
        """`$nome` non apre un turno nemmeno dentro il limite (R12): oltre il
        limite non c'è niente di negato da dichiarare."""
        with patch.dict(os.environ, {"CHANNEL_SOFT_ACK_RATE": "0"}):
            posts, start = await self._delegate(
                "Per conoscenza $fullstack-dev", hop=channels._max_delegation_hops())
        start.assert_not_awaited()
        self.assertEqual([], posts)


class TheLimitIsConfigurableTests(_Base):
    """Il valore giusto dipende da quanti agenti collaborano, e non lo sappiamo
    a priori: con un coordinatore e un esecutore, `2` esaurisce la catena al
    primo scambio di ritorno."""

    async def test_the_env_var_raises_it(self) -> None:
        with patch.dict(os.environ, {"CLODIA_MAX_DELEGATION_HOPS": "6"}):
            self.assertEqual(6, channels._max_delegation_hops())
            posts, start = await self._delegate("Ci pensa @fullstack-dev", hop=4)
        self.assertEqual(1, start.await_count)

    async def test_a_bad_value_falls_back_to_the_default(self) -> None:
        """Un valore illeggibile non deve spegnere il freno né rompere il turno."""
        for cattivo in ("", "molti", "-3"):
            with self.subTest(valore=cattivo):
                with patch.dict(os.environ,
                                {"CLODIA_MAX_DELEGATION_HOPS": cattivo}):
                    self.assertEqual(channels._DEFAULT_MAX_DELEGATION_HOPS,
                                     channels._max_delegation_hops())


if __name__ == "__main__":
    unittest.main()
