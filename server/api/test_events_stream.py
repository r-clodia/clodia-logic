"""Lo stream SSE consegna solo le stanze che chi ascolta ha diritto di vedere.

Era un broadcast globale **senza autenticazione**: nato aperto perché
`EventSource` non manda header, e rimasto tale mentre gli eventi si arricchivano
del testo dei messaggi. Chi raggiungeva la porta leggeva le conversazioni di
ogni topic, tier alti inclusi.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import agents


class _Ev:
    def __init__(self, payload):
        self.payload = payload


def _meta(owner="davide", participants=("clodia", "clodia-primal")):
    return {"owner": owner, "participants": list(participants)}


class EventVisibilityTests(unittest.TestCase):
    def test_a_room_event_reaches_a_participant(self):
        with patch.object(agents, "_room_meta", lambda *_a: _meta()):
            self.assertTrue(agents._event_visible(
                "clodia-primal", True,
                _Ev({"tier": "SEAL-1", "name": "acme", "text": "segreto"})))

    def test_a_room_event_does_not_reach_an_outsider(self):
        """Il caso che il broadcast aperto serviva a chiunque."""
        with patch.object(agents, "_room_meta", lambda *_a: _meta()):
            self.assertFalse(agents._event_visible(
                "estraneo", False,
                _Ev({"tier": "SEAL-1", "name": "acme", "text": "segreto"})))

    def test_an_unreadable_room_delivers_nothing(self):
        with patch.object(agents, "_room_meta", lambda *_a: None):
            self.assertFalse(agents._event_visible(
                "clodia", False, _Ev({"tier": "SEAL-3", "name": "x"})))

    def test_events_without_a_room_stay_for_the_webui(self):
        self.assertTrue(agents._event_visible(
            "davide", False, _Ev({"agent": "clodia", "state": "busy"})))

    def test_a_proxy_gets_nothing_outside_its_rooms(self):
        """Un sistema terzo vede la stanza in cui è stato ammesso, e basta."""
        self.assertFalse(agents._event_visible(
            "clodia-primal", True, _Ev({"agent": "clodia", "state": "busy"})))


class StreamPrincipalTests(unittest.TestCase):
    class _Req:
        def __init__(self, headers=None, query=None):
            self.headers = headers or {}
            self.query_params = query or {}

    def test_no_token_no_stream(self):
        chi, _, _stanza = agents._stream_principal(self._Req())
        self.assertIsNone(chi)

    def test_the_token_may_come_from_the_query_because_eventsource_cannot_send_headers(self):
        with patch("server.colony.pki.verify_session_token",
                   lambda _t: {"principal": "davide", "agent": "clodia"}), \
             patch.object(agents.registry, "get_by_name", lambda _n: None):
            chi, is_proxy, _stanza = agents._stream_principal(
                self._Req(query={"token": "ckt1.x"}))
        self.assertEqual(chi, "davide")
        self.assertFalse(is_proxy)

    def test_the_principal_wins_over_the_carrier(self):
        """Un token di proxy è firmato dal carrier `clodia`, che partecipa quasi
        ovunque: filtrare sul carrier darebbe al proxy la vista di clodia."""
        class _Spec:
            type = "proxy"

        with patch("server.colony.pki.verify_session_token",
                   lambda _t: {"principal": "clodia-primal", "agent": "clodia"}), \
             patch.object(agents.registry, "get_by_name", lambda _n: _Spec()):
            chi, is_proxy, _stanza = agents._stream_principal(
                self._Req(headers={"authorization": "Bearer ckt1.x"}))
        self.assertEqual(chi, "clodia-primal")
        self.assertTrue(is_proxy)


if __name__ == "__main__":
    unittest.main()
