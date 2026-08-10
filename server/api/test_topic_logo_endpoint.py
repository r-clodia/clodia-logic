"""L'immagine di un topic: la vede chi partecipa, la cambia solo l'owner.

Non è una decorazione. In una lista di venti stanze l'immagine è ciò che si
guarda per primo, quindi cambiarla è un modo di far sembrare una stanza un'altra:
è un atto sui muri, come aggiungere un partecipante o collegare un gruppo
Telegram, e lo decide chi possiede la stanza.

*Perché un test e non una prova sul campo.* Il token umano lo firma la chiave
della persona, che sta **nel browser**: dal server non è forgiabile — ed è una
proprietà, non un ostacolo. Un tentativo di collaudo da shell è passato per la
ragione sbagliata (il principal si risolveva al carrier, non alla persona), che è
esattamente il modo in cui una prova verde certifica il nulla. Qui il principal è
esplicito.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from . import topics as T


META = {"tier": "SEAL-1", "owner": "davide",
        "participants": {"davide": "owner", "giovanni": "member"}}
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
           "DwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class _Req:
    def __init__(self, body=None):
        self._b = body or {}

    async def json(self):
        return self._b


class _Client:
    class TopicsClientError(RuntimeError):
        pass

    def __init__(self, meta=None):
        self.meta = meta if meta is not None else META
        self.chiesto = "non chiamato"

    def open_topic(self, tier, name):
        return {"meta": self.meta}

    def topic_logo(self, tier, name, payload):
        self.chiesto = payload
        return {"logo": "files/.brand/logo"}

    def read_topic_logo(self, tier, name):
        # Rotta DEDICATA, non quella generica dei file: il logo sta nel control
        # plane, mentre `/file` risolve i path del data plane — su un topic con
        # remote Drive lo cercherebbe su Drive, dove non è mai stato scritto.
        return b"\x89PNG\r\n\x1a\n", self.meta.get("logo_kind") or "image/png"


class OnlyTheOwnerChangesItTests(unittest.TestCase):
    def test_the_owner_may_set_it(self):
        import asyncio as _a
        with patch.object(T, "topics_client", _Client()), \
             patch.object(T, "_principal_from_request", lambda r: "davide"), \
             patch.object(T.admin, "is_admin", lambda n: False):
            res = _a.run(T.set_topic_logo("SEAL-1", "acme", _Req({"data": PNG_B64})))
        self.assertIn("logo", res)

    def test_a_participant_may_not(self):
        """Il caso che dà senso alla regola: Giovanni partecipa, e non decide
        come si presenta la stanza di Davide."""
        import asyncio
        with patch.object(T, "topics_client", _Client()), \
             patch.object(T, "_principal_from_request", lambda r: "giovanni"), \
             patch.object(T.admin, "is_admin", lambda n: False):
            with self.assertRaises(HTTPException) as e:
                asyncio.run(T.set_topic_logo("SEAL-1", "acme", _Req({"data": PNG_B64})))
        self.assertEqual(e.exception.status_code, 403)

    def test_a_participant_may_not_remove_it_either(self):
        with patch.object(T, "topics_client", _Client()), \
             patch.object(T, "_principal_from_request", lambda r: "giovanni"), \
             patch.object(T.admin, "is_admin", lambda n: False):
            with self.assertRaises(HTTPException) as e:
                T.clear_topic_logo("SEAL-1", "acme", _Req())
        self.assertEqual(e.exception.status_code, 403)

    def test_an_anonymous_request_is_refused(self):
        import asyncio
        with patch.object(T, "topics_client", _Client()), \
             patch.object(T, "_principal_from_request", lambda r: None):
            with self.assertRaises(HTTPException) as e:
                asyncio.run(T.set_topic_logo("SEAL-1", "acme", _Req({"data": PNG_B64})))
        self.assertIn(e.exception.status_code, (401, 403))


class AnyoneInTheRoomSeesItTests(unittest.TestCase):
    def test_a_participant_sees_it(self):
        meta = dict(META, logo="files/.brand/logo", logo_kind="image/png")
        with patch.object(T, "topics_client", _Client(meta)), \
             patch.object(T, "_principal_from_request", lambda r: "giovanni"), \
             patch.object(T.admin, "is_admin", lambda n: False):
            r = T.get_topic_logo("SEAL-1", "acme", _Req())
        self.assertEqual(r.media_type, "image/png")

    def test_someone_outside_the_room_does_not(self):
        meta = dict(META, logo="files/.brand/logo")
        with patch.object(T, "topics_client", _Client(meta)), \
             patch.object(T, "_principal_from_request", lambda r: "estraneo"), \
             patch.object(T.admin, "is_admin", lambda n: False):
            with self.assertRaises(HTTPException) as e:
                T.get_topic_logo("SEAL-1", "acme", _Req())
        self.assertEqual(e.exception.status_code, 403)

    def test_no_logo_is_a_404_not_an_error(self):
        with patch.object(T, "topics_client", _Client()), \
             patch.object(T, "_principal_from_request", lambda r: "davide"), \
             patch.object(T.admin, "is_admin", lambda n: False):
            with self.assertRaises(HTTPException) as e:
                T.get_topic_logo("SEAL-1", "acme", _Req())
        self.assertEqual(e.exception.status_code, 404)

    def test_the_type_comes_from_the_meta_not_from_a_guess(self):
        """Il file non ha estensione: il tipo lo sa solo chi ha guardato i byte
        al caricamento. Indovinarlo qui significherebbe dichiararne uno che può
        essere falso."""
        meta = dict(META, logo="files/.brand/logo", logo_kind="image/webp")
        with patch.object(T, "topics_client", _Client(meta)), \
             patch.object(T, "_principal_from_request", lambda r: "davide"), \
             patch.object(T.admin, "is_admin", lambda n: False):
            r = T.get_topic_logo("SEAL-1", "acme", _Req())
        self.assertEqual(r.media_type, "image/webp")


if __name__ == "__main__":
    unittest.main()
