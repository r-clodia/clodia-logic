"""HTTP verso il gateway interno, con circuit breaker.

Quando il gateway non risponde, il costo non è la singola chiamata fallita: è che
ogni chiamata successiva paga il proprio timeout intero prima di arrendersi. Con
decine di handler che parlano col gateway, un gateway irraggiungibile diventa una
coda di thread in attesa — l'incidente del 17 lug visto dall'altro lato.

Il breaker conta i fallimenti **di connessione** consecutivi (`RequestException`:
connessione rifiutata, DNS, timeout) e dopo `threshold` smette di provare per
`cooldown` secondi, fallendo subito. Una risposta HTTP — anche 500 — NON è un
fallimento di connessione: il gateway c'è e ha risposto, e le policy di errore
stanno nei client.

`CircuitOpen` deriva da `requests.RequestException` di proposito: i client
esistenti catturano già quella classe e la traducono nel loro errore
«gateway irraggiungibile». Il fail-fast entra così nel percorso di errore che
c'è già, senza un secondo modo di fallire da gestire in ogni chiamante.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

LOG = logging.getLogger("agent-server.gateway_http")

_THRESHOLD = 5      # fallimenti di connessione consecutivi prima di aprire
_COOLDOWN = 10.0    # secondi di fail-fast prima di riprovare (una sonda)


class CircuitOpen(requests.RequestException):
    """Il gateway è dato per irraggiungibile: si fallisce subito."""


class GatewayHTTP:
    """Proxy sui verbi di `requests` per un servizio interno.

    Uso: `_http = GatewayHTTP("topics")`, poi `_http.get(url, ...)` al posto di
    `requests.get(url, ...)`. Firma identica, così il passaggio è una
    sostituzione di nome e non una riscrittura dei chiamanti.
    """

    def __init__(self, name: str, threshold: int = _THRESHOLD,
                 cooldown: float = _COOLDOWN):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    # ── stato ────────────────────────────────────────────────────────────────
    def _before(self) -> None:
        """Solleva CircuitOpen se il circuito è aperto e il cooldown non è finito.

        Scaduto il cooldown si lascia passare UNA richiesta (mezza apertura): se
        va bene il circuito si chiude, se fallisce si riapre per un altro giro.
        """
        with self._lock:
            if self._open_until and time.monotonic() < self._open_until:
                resta = self._open_until - time.monotonic()
                raise CircuitOpen(
                    f"gateway '{self.name}' dato per irraggiungibile dopo "
                    f"{self._failures} errori di connessione: riprovo fra {resta:.0f}s")
            # cooldown finito (o circuito mai aperto): si prova
            self._open_until = 0.0

    def _success(self) -> None:
        with self._lock:
            if self._failures:
                LOG.info("gateway '%s' di nuovo raggiungibile", self.name)
            self._failures = 0
            self._open_until = 0.0

    def _failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = time.monotonic() + self.cooldown
                LOG.warning("gateway '%s' irraggiungibile da %d chiamate: "
                            "fail-fast per %.0fs", self.name, self._failures,
                            self.cooldown)

    # ── verbi ────────────────────────────────────────────────────────────────
    def request(self, method: str, url: str, **kwargs):
        self._before()
        try:
            r = requests.request(method, url, **kwargs)
        except requests.RequestException:
            self._failure()
            raise
        self._success()
        return r

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)
