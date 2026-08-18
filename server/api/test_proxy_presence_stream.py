"""La presenza di un proxy è la sua CONNESSIONE, non un battito che ricorda lui.

clodia-platform#218. Misurato il 18 ago 2026, collegando il proxy reale
`clodia-primal` a `SEAL-1/risoluzione-issue-clodia` e leggendo la stanza: in
`presence.json` c'era **solo `davide`**, zero voci per il proxy.

La catena, per intero, perché il difetto sta in un pezzo che manca e non in uno
sbagliato:

- `presence.touch` è chiamata SOLO da `channel_messages`, cioè dall'endpoint
  dell'agent-server che la webui interroga;
- un proxy non passa da lì: legge con i verbi del gateway (`topic.messages`,
  `topic.my_mentions`), che leggono il topic store;
- il gateway `presence.json` lo **legge** (per sopprimere le notifiche Telegram)
  e non lo scrive mai.

Quindi mostrare il pallino di un proxy (#247) era necessario e non sufficiente:
il pallino c'era e non poteva accendersi, perché nessuno emetteva il battito. Un
canale di visualizzazione senza sorgente è indistinguibile da «quel proxy non
c'è mai».

Il battito ora nasce dallo stream SSE, che il proxy tiene aperto per ascoltare:
tenerlo È essere presente, e un ponte che muore lo lascia cadere da sé. È la
forma che #218 chiede — un ping periodico può mentire, perché continua ad
arrivare da un processo che non ascolta più nessuno.

IL VINCOLO DA NON PERDERE, e la ragione per cui questo non vale per gli umani:
uno stream resta aperto anche con la scheda in secondo piano. Battere da lì per
una persona renderebbe `background` irraggiungibile — cioè cancellerebbe una
delle quattro risposte che la presenza esiste per distinguere.
"""
from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest.mock import patch

from . import agents, presence


class LaStanzaVieneDalTokenTests(unittest.TestCase):
    """La stanza è parte di ciò che è stato firmato: non si prende altrove."""

    def test_the_room_comes_from_the_chat_claim(self) -> None:
        self.assertEqual(
            ("SEAL-1", "risoluzione-issue-clodia"),
            agents._stanza_dal_token(
                {"chat": "chan:SEAL-1:risoluzione-issue-clodia:clodia-primal"}))

    def test_no_chat_claim_no_room(self) -> None:
        for payload in ({}, {"chat": ""}, {"chat": "spawn:clodia-4"},
                        {"chat": "chan:SEAL-1"}, None):
            with self.subTest(payload=payload):
                self.assertIsNone(agents._stanza_dal_token(payload))

    def test_a_room_is_not_taken_from_a_query_parameter(self) -> None:
        """Se la stanza si potesse dichiarare fuori dalla firma, un proxy
        annuncerebbe presenza in stanze in cui non è stato ammesso."""
        src = inspect.getsource(agents._stanza_dal_token)
        self.assertNotIn("query_params", src)


class IlBattitoDuraQuantoLaConnessioneTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_beats_at_once_and_keeps_beating(self) -> None:
        battiti: list[tuple] = []
        with patch.object(presence, "touch",
                          lambda chi, t, n: battiti.append((chi, t, n))), \
                patch.object(agents, "_PROXY_BEAT_EVERY_S", 0.01):
            task = asyncio.create_task(
                agents._batti_finche_ascolta("clodia-primal", "SEAL-1", "acme"))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertGreaterEqual(len(battiti), 2, "un battito solo scade col TTL")
        self.assertEqual(("clodia-primal", "SEAL-1", "acme"), battiti[0])

    async def test_the_period_stays_under_the_ttl(self) -> None:
        """Con una cadenza oltre `TTL_S` un proxy connesso lampeggerebbe fra
        presente e assente — peggio che non mostrarlo, perché sembra un guasto."""
        self.assertLess(agents._PROXY_BEAT_EVERY_S, presence.TTL_S)

    async def test_a_failing_beat_does_not_kill_the_stream(self) -> None:
        def rotto(*_a, **_k):
            raise RuntimeError("presence.json non scrivibile")

        with patch.object(presence, "touch", rotto), \
                patch.object(agents, "_PROXY_BEAT_EVERY_S", 0.01):
            # non deve propagare: la presenza è un accessorio dello stream
            await asyncio.wait_for(
                agents._batti_finche_ascolta("clodia-primal", "SEAL-1", "acme"),
                timeout=1.0)


class SoloIProxyTests(unittest.TestCase):
    """Il confine che protegge i quattro stati degli umani."""

    def test_the_stream_beats_only_for_a_proxy(self) -> None:
        src = inspect.getsource(agents.events)
        self.assertIn("if is_proxy and stanza:", src,
                      "il battito dello stream non è più condizionato al proxy: "
                      "per un umano `background` diventa irraggiungibile")

    def test_the_beat_is_cancelled_when_the_stream_ends(self) -> None:
        """Senza il cancel, un ponte terminato resterebbe «presente» per sempre —
        che è il difetto che #218 descrive, al contrario."""
        src = inspect.getsource(agents.events)
        self.assertIn("battito.cancel()", src)
        self.assertIn("finally:", src)


if __name__ == "__main__":
    unittest.main()
