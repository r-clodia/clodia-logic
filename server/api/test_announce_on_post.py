"""L'annuncio appartiene all'ATTO DI POSTARE, non a una delle due porte
(clodia-platform#219).

Un messaggio scritto **attraverso il gateway** — il proxy, il messaggero, il
client MCP di una persona, un job — veniva persistito e, se portava una
menzione, faceva partire un turno: l'unico effetto visibile. Sul bus SSE non
compariva nulla, perché `channel_message` lo pubblicava `_channel_message`,
cioè *una* delle due porte, invece del punto in cui il messaggio nasce. Due
proxy nella stessa stanza non potevano sentirsi, e un bridge in ascolto sullo
stream (#218) perdeva tutto ciò che scrive il messaggero.

Qui si coprono la porta che prima non annunciava e le proprietà che rendono
sicuro il passaggio: l'**idempotenza per `id`** — nella finestra fra i due
deploy annunciano entrambe le porte, e senza dedup il bus emetterebbe due
`channel_message` per lo stesso messaggio — e il riconoscimento del chiamante.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels as ch

SECRET = "CLODIA_ORCHESTRATOR_SECRET"


def _env(secret: str | None):
    """Ambiente con il secret orchestrator IMPOSTATO o ASSENTE, di proposito.

    L'ambiente reale lo ha (compose lo passa a entrambi i servizi, e gli spawn
    lo ereditano): un test che non lo controlla misura la macchina su cui gira,
    non il codice — ed è così che una suite diventa verde o rossa a seconda di
    dove la lanci.
    """
    env = {k: v for k, v in os.environ.items() if k != SECRET}
    if secret:
        env[SECRET] = secret
    return patch.dict(os.environ, env, clear=True)


def _request(body: dict, *, secret: str | None = None):
    """Finta `Request`: solo `json()` e `headers`, i due soli campi che la rotta
    legge. Un TestClient monterebbe l'app intera per esercitare una rotta che
    non ha dipendenze dal middleware."""
    async def _json():
        return body

    req = type("R", (), {})()
    req.json = _json
    req.headers = {"x-orchestrator-secret": secret} if secret else {}
    return req


MSG = {
    "id": "20260907-120000-abc",
    "ts": "2026-09-07T12:00:00.000100+00:00",
    "text": "@clodia guarda qui",
    "mentions": ["Clodia"],
    "author": "messaggero",
    "kind": "ai",
}


class AnnounceOnPostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # La finestra di dedup è stato di modulo: senza azzerarla sarebbe
        # l'ordine dei test a decidere l'esito.
        ch._ANNOUNCED_IDS.clear()

    async def _announce(self, body: dict, *, server_secret: str | None = None,
                        header: str | None = None, title: str = "Software House"):
        publish = AsyncMock()
        with _env(server_secret), patch.object(ch.bus, "publish", publish), \
                patch.object(ch.topics_client, "open_topic",
                             return_value={"meta": {"title": title}}):
            out = await ch.channel_announce_internal(
                "SEAL-1", "software-house", _request(body, secret=header))
        return out, publish

    async def test_a_message_written_through_the_gateway_reaches_the_bus(self) -> None:
        out, publish = await self._announce(dict(MSG))

        self.assertTrue(out["announced"])
        event = publish.await_args.args[0]
        self.assertEqual(event.type, "channel_message")
        self.assertEqual(event.payload["tier"], "SEAL-1")
        self.assertEqual(event.payload["name"], "software-house")
        self.assertEqual(event.payload["author"], "messaggero")
        self.assertEqual(event.payload["kind"], "ai")
        self.assertEqual(event.payload["id"], MSG["id"])
        self.assertEqual(event.payload["ts"], MSG["ts"])
        self.assertEqual(event.payload["text"], MSG["text"])
        # minuscole come `_channel_message`: la scala di presenza confronta nomi
        # normalizzati, e due normalizzazioni diverse sono due comportamenti
        self.assertEqual(event.payload["mentions"], ["clodia"])
        # riletto lato server, non creduto dal body: il titolo è del topic
        self.assertEqual(event.payload["topic_title"], "Software House")

    async def test_the_same_id_is_announced_only_once(self) -> None:
        first, publish1 = await self._announce(dict(MSG))
        second, publish2 = await self._announce(dict(MSG))

        self.assertTrue(first["announced"])
        self.assertFalse(second["announced"])
        self.assertEqual(publish1.await_count, 1)
        self.assertEqual(publish2.await_count, 0)

    async def test_the_other_door_does_not_duplicate_the_same_message(self) -> None:
        """La proprietà che rende sicuro QUALUNQUE ordine di deploy fra i due
        repo: finché `_channel_message` continua ad annunciare, il suo evento
        viene soppresso perché quello del gateway è già passato."""
        _, publish = await self._announce(dict(MSG))
        self.assertEqual(publish.await_count, 1)

        seconda_porta = AsyncMock()
        with patch.object(ch.bus, "publish", seconda_porta), \
                patch.object(ch.access_log, "touch"):
            await ch._channel_message("SEAL-1", "software-house", "messaggero",
                                      "ai", message=dict(MSG),
                                      topic_title="Software House")

        self.assertEqual(seconda_porta.await_count, 0)

    async def test_a_message_without_id_is_still_announced(self) -> None:
        """Senza `id` non c'è niente da deduplicare: sopprimere sarebbe peggio
        del doppione — una bolla che non compare a nessuno."""
        senza = {k: v for k, v in MSG.items() if k != "id"}
        out, publish = await self._announce(senza)

        self.assertTrue(out["announced"])
        self.assertEqual(publish.await_count, 1)

    async def test_an_unknown_topic_is_not_announced(self) -> None:
        publish = AsyncMock()
        with _env(None), patch.object(ch.bus, "publish", publish), \
                patch.object(ch.topics_client, "open_topic", return_value=None):
            with self.assertRaises(ch.HTTPException) as e:
                await ch.channel_announce_internal(
                    "SEAL-1", "non-esiste", _request(dict(MSG)))

        self.assertEqual(e.exception.status_code, 404)
        self.assertEqual(publish.await_count, 0)

    async def test_an_announcement_without_the_shared_secret_is_refused(self) -> None:
        """Quando il server HA un secret configurato, la verifica è vincolante."""
        with self.assertRaises(ch.HTTPException) as e:
            await self._announce(dict(MSG), server_secret="s3cr3t",
                                 header="sbagliato")
        self.assertEqual(e.exception.status_code, 403)

        out, publish = await self._announce(dict(MSG), server_secret="s3cr3t",
                                            header="s3cr3t")
        self.assertTrue(out["announced"])
        self.assertEqual(publish.await_count, 1)

    async def test_without_a_configured_secret_the_announcement_passes(self) -> None:
        """Fail-OPEN, non fail-closed: `docker-compose.yml` passa il secret come
        `${...:-}`, quindi può essere vuoto — e un fail-closed spegnerebbe
        l'annuncio *in silenzio*, cioè rimetterebbe il difetto di questa issue."""
        out, publish = await self._announce(dict(MSG), server_secret=None)

        self.assertTrue(out["announced"])
        self.assertEqual(publish.await_count, 1)


if __name__ == "__main__":
    unittest.main()
