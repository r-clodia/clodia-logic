import unittest
from unittest.mock import patch

import requests

from server.api.gateway_http import CircuitOpen, GatewayHTTP


class _Resp:
    status_code = 200


class GatewayHTTPBreakerTests(unittest.TestCase):
    def test_apre_dopo_la_soglia_e_non_chiama_piu_la_rete(self):
        http = GatewayHTTP("test", threshold=3, cooldown=60.0)
        with patch.object(requests, "request",
                          side_effect=requests.ConnectionError("boom")) as req:
            for _ in range(3):
                with self.assertRaises(requests.ConnectionError):
                    http.get("http://gw/x")
            # la quarta non arriva alla rete: fallisce subito
            with self.assertRaises(CircuitOpen):
                http.get("http://gw/x")
        self.assertEqual(req.call_count, 3)

    def test_circuit_open_e_un_requestexception(self):
        """I client traducono già `RequestException` in «gateway irraggiungibile»:
        il fail-fast deve entrare in quel percorso, non in uno nuovo."""
        self.assertTrue(issubclass(CircuitOpen, requests.RequestException))

    def test_una_risposta_http_non_e_un_fallimento_di_connessione(self):
        http = GatewayHTTP("test", threshold=2, cooldown=60.0)
        with patch.object(requests, "request", return_value=_Resp()) as req:
            for _ in range(5):
                self.assertEqual(http.get("http://gw/x").status_code, 200)
        self.assertEqual(req.call_count, 5)

    def test_successo_azzera_il_contatore(self):
        http = GatewayHTTP("test", threshold=3, cooldown=60.0)
        with patch.object(requests, "request",
                          side_effect=[requests.ConnectionError("1"),
                                       requests.ConnectionError("2"),
                                       _Resp(),
                                       requests.ConnectionError("3"),
                                       requests.ConnectionError("4"),
                                       _Resp()]):
            for _ in range(2):
                with self.assertRaises(requests.ConnectionError):
                    http.get("http://gw/x")
            http.get("http://gw/x")
            for _ in range(2):
                with self.assertRaises(requests.ConnectionError):
                    http.get("http://gw/x")
            # ancora sotto soglia grazie all'azzeramento: passa
            self.assertEqual(http.get("http://gw/x").status_code, 200)

    def test_dopo_il_cooldown_una_sonda_passa_e_il_successo_richiude(self):
        http = GatewayHTTP("test", threshold=1, cooldown=0.0)
        with patch.object(requests, "request",
                          side_effect=[requests.ConnectionError("giù"), _Resp()]) as req:
            with self.assertRaises(requests.ConnectionError):
                http.get("http://gw/x")
            self.assertEqual(http.get("http://gw/x").status_code, 200)
        self.assertEqual(req.call_count, 2)
        self.assertEqual(http._failures, 0)

    def test_il_verbo_arriva_a_requests_request(self):
        http = GatewayHTTP("test")
        with patch.object(requests, "request", return_value=_Resp()) as req:
            http.delete("http://gw/x", timeout=5)
        req.assert_called_once_with("DELETE", "http://gw/x", timeout=5)


if __name__ == "__main__":
    unittest.main()
