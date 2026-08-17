"""Il token di sessione si conia UNA volta sola anche sotto concorrenza (#106).

L'offload dei client del gateway su `asyncio.to_thread` porta più thread dentro
`pki.mint_session_token` nello stesso istante: il ciclo «leggi scadenza → conia →
scrivi in cache», prima atomico perché girava solo sull'event loop, diventa una
gara.

Il test la rende DETERMINISTICA invece di sperare nella sfortuna: N thread
rilasciati insieme da una `threading.Barrier` con la cache scaduta/vuota, e un
mint fittizio che dorme dentro la finestra critica. Senza il lock per-chiave il
contatore dei mint arriva a N (rosso); con il lock resta 1 (verde).
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

from server.colony import pki

THREADS = 8
MINT_DELAY = 0.05  # allarga la finestra critica: la gara si riproduce sempre


class _FakeResponse:
    """Risposta minima del gateway di mint."""

    status_code = 200
    text = ""

    def __init__(self, token: str) -> None:
        self._token = token

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"token": self._token}


class MintConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._reset_cache()
        self.addCleanup(self._reset_cache)
        self.lock = threading.Lock()
        self.mint_calls: list[dict] = []

    @staticmethod
    def _reset_cache() -> None:
        with pki._MINT_STATE_LOCK:
            pki._MINT_CACHE.clear()
            pki._MINT_KEY_LOCKS.clear()

    def _fake_post(self, url, json=None, headers=None, timeout=None):
        with self.lock:
            self.mint_calls.append(json)
            n = len(self.mint_calls)
        time.sleep(MINT_DELAY)
        return _FakeResponse(f"tok-{n}")

    def _race(self, threads: int = THREADS) -> tuple[list[str], list[BaseException]]:
        """Rilascia `threads` thread insieme su `mint_session_token`."""
        barrier = threading.Barrier(threads)
        tokens: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                token = pki.mint_session_token("clodia", ttl_seconds=300)
                with self.lock:
                    tokens.append(token)
            except BaseException as e:  # noqa: BLE001 — riportato all'assert
                errors.append(e)

        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "test-secret"}):
            with patch("httpx.post", side_effect=self._fake_post):
                pool = [threading.Thread(target=worker) for _ in range(threads)]
                for t in pool:
                    t.start()
                for t in pool:
                    t.join(timeout=30)
        return tokens, errors

    def test_cache_vuota_un_solo_mint(self) -> None:
        tokens, errors = self._race()

        self.assertEqual(errors, [], "nessun thread deve fallire il mint")
        self.assertEqual(len(self.mint_calls), 1,
                         "cache vuota + N thread ⇒ il token va coniato una volta sola")
        self.assertEqual(len(tokens), THREADS)
        self.assertEqual(set(tokens), {"tok-1"},
                         "tutti i thread devono condividere lo stesso token")

    def test_cache_scaduta_un_solo_re_mint(self) -> None:
        # Primo mint (single-thread) per popolare la cache…
        tokens, errors = self._race(threads=1)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.mint_calls), 1)

        # …poi la si porta a scadenza: la finestra di rinnovo è quella pericolosa.
        with pki._MINT_STATE_LOCK:
            self.assertEqual(len(pki._MINT_CACHE), 1)
            key = next(iter(pki._MINT_CACHE))
            pki._MINT_CACHE[key] = (tokens[0], int(time.time()) - 1)

        tokens, errors = self._race()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.mint_calls), 2,
                         "il rinnovo di una cache scaduta è un mint solo, non N")
        self.assertEqual(set(tokens), {"tok-2"})

    def test_chiavi_diverse_non_si_serializzano_sulla_stessa_voce(self) -> None:
        """Identità diverse ⇒ chiavi diverse ⇒ un mint ciascuna (nessuna fusione)."""
        with patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": "test-secret"}):
            with patch("httpx.post", side_effect=self._fake_post):
                a = pki.mint_session_token("clodia", ttl_seconds=300)
                b = pki.mint_session_token("clodia", execution_id="exec-2",
                                           ttl_seconds=300)

        self.assertEqual(len(self.mint_calls), 2)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
