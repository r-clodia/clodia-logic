"""Il meccanismo A (bind manuale di un gruppo Telegram) non ha più residui qui.

clodia-platform#361. Il meccanismo A muore in clodia-tools#280: spariscono i
verbi `topic.telegram_bind`/`telegram_unbind` e la route interna
`POST /internal/topics/{tier}/{name}/telegram`. Qui restavano il proxy verso
quella route e il suo unico client: senza il lato gateway risponderebbero solo
502, quindi vanno via anche loro.

Questi test sarebbero ROSSI prima della rimozione: la route e il client c'erano.
L'ultimo è il più importante: il binding NON muore, cambia solo chi lo scrive
(`telegram.listen`, l'ingress di clodia-tools#364). Chi grep-pa
«telegram-bindings» e cancella tutto ciò che trova spegne l'ingress appena
costruito — questo test dice, in modo eseguibile, cosa resta in piedi.
"""
from __future__ import annotations

import unittest

from . import topics, topics_client


def _paths() -> set[str]:
    return {getattr(r, "path", "") for r in topics.router.routes}


class RottaProxyRimossaTests(unittest.TestCase):
    def test_nessuna_rotta_telegram_sui_topic(self) -> None:
        residue = [p for p in _paths() if p.endswith("/telegram")]
        self.assertEqual(residue, [])

    def test_il_client_non_espone_piu_il_binding(self) -> None:
        for nome in ("telegram_binding", "async_telegram_binding"):
            self.assertFalse(hasattr(topics_client, nome), nome)


class IngressSopravviveTests(unittest.TestCase):
    """Il meccanismo B (`telegram.listen` → `telegram-bindings.json`) resta."""

    def test_il_client_dei_binding_resta(self) -> None:
        from . import telegram_bindings_client

        self.assertTrue(hasattr(telegram_bindings_client, "load"))

    def test_il_relay_di_canale_resta(self) -> None:
        from . import channel_relay

        self.assertTrue(hasattr(channel_relay, "run_poll_cycle"))


if __name__ == "__main__":
    unittest.main()
