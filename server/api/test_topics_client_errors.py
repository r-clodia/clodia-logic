"""Un rifiuto del gateway resta un rifiuto (clodia-platform, seguito di #115).

Il caso reale: collegare Drive a un topic con 18 file locali viene rifiutato dal
gateway con 400 e un messaggio che dice cosa fare. Arrivava alla UI come
`HTTP 502 — {"detail":"gateway remote → HTTP 400: {\\"error\\":\\"collegare Drive…`
troncato a metà: la classe dell'errore era persa e il testo annidato due volte.
"""
from __future__ import annotations

import unittest

from . import topics_client


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._p = payload
        self.text = text

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


class HttpErrorTests(unittest.TestCase):
    def test_a_4xx_keeps_its_status_and_message(self):
        e = topics_client._http_error("remote", _Resp(
            400, {"error": "collegare Drive nasconderebbe 18 file locali"}))
        self.assertTrue(e.is_client_error)
        self.assertEqual(e.status, 400)
        self.assertEqual(e.detail, "collegare Drive nasconderebbe 18 file locali")

    def test_the_message_is_not_a_nested_json_string(self):
        """Il difetto visibile all'utente: il JSON del gateway finiva dentro la
        stringa dell'errore, che finiva dentro il detail dell'HTTPException."""
        e = topics_client._http_error("remote", _Resp(400, {"error": "motivo chiaro"}))
        self.assertNotIn('{"', e.detail)
        self.assertNotIn("\\\\", e.detail)

    def test_a_5xx_is_not_a_client_error(self):
        e = topics_client._http_error("remote", _Resp(500, {"error": "boom"}))
        self.assertFalse(e.is_client_error)

    def test_a_non_json_body_falls_back_to_the_text(self):
        e = topics_client._http_error("remote", _Resp(502, None, text="Bad Gateway"))
        self.assertEqual(e.detail, "Bad Gateway")

    def test_an_error_raised_without_a_response_has_no_status(self):
        """Gli altri call-site sollevano ancora `TopicsClientError(msg)`: non
        devono rompersi né essere confusi con un rifiuto del gateway."""
        e = topics_client.TopicsClientError("gateway irraggiungibile")
        self.assertIsNone(e.status)
        self.assertFalse(e.is_client_error)
        self.assertEqual(e.detail, "gateway irraggiungibile")


if __name__ == "__main__":
    unittest.main()
