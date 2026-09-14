"""Il meccanismo A di Telegram non c'è più, e questo file è la prova che non torna.

Fratello di `server/test_removed_workflow_scope.py`, stessa forma: là cadeva il
namespace `workflows.`, qui cade tutto ciò che da questo lato nominava ancora il
**meccanismo A** — il «collegamento» di un gruppo a un topic
(`topic.telegram_bind` / `topic.telegram_unbind`) e la notifica-su-menzione che
ci stava sopra (`telegram.notify_pending` / `telegram.notify_ack`).

Quei verbi il gateway non li ha più: `clodia-tools` li ha rimossi con
clodia-platform#360 (PR clodia-tools#280), insieme alla rotta interna
`POST /internal/topics/{tier}/{name}/telegram` e a `server/topics/telegram_notify.py`.
Da questo lato restavano un proxy HTTP verso quella rotta e — il pezzo che morde
di più — una SKILL che insegnava al messaggero a chiamare due verbi inesistenti:
un seed che promette una capacità che non c'è la rilegge a ogni turno, e l'agente
ci prova.

**Cosa NON cerca, di proposito.** `telegram-bindings.json` e chi lo legge
(`server/api/telegram_bindings_client.py`, `server/api/channel_relay.py`) sono
VIVI: il file sopravvive al meccanismo A, cambia solo chi lo scrive — ora
`telegram.listen`, gated sugli ingress del topic (clodia-platform#364). Per
questo il criterio nomina i VERBI e la rotta, non la parola «binding»: un grep
su «telegram-bindings» che cancella tutto ciò che trova spegne l'ingress appena
costruito.
"""
from __future__ import annotations

import pathlib
import unittest

RADICE = pathlib.Path(__file__).parent
CATALOGHI = RADICE.parent / "catalogs"

#: I verbi del meccanismo A, come li scriveva chi li usava, e la skill che ci
#: stava sopra.
RESIDUI = (
    "topic.telegram_bind",
    "topic.telegram_unbind",
    "telegram.notify_pending",
    "telegram.notify_ack",
    "mention-relay",
    "mention_relay",
)

#: File che parlano della rimozione invece di implementarla: la memoria di
#: perché una cosa non c'è più è utile e non è un residuo.
ESENTI = {"test_removed_telegram_bind.py", "CHANGELOG.md"}


def _sorgenti():
    for radice, pattern in ((RADICE, "*.py"), (CATALOGHI, "*.md"),
                            (CATALOGHI, "*.yaml")):
        for f in radice.rglob(pattern):
            if f.name in ESENTI or "__pycache__" in f.parts:
                continue
            yield f


class NoMeccanismoATests(unittest.TestCase):
    def test_no_source_mentions_the_dead_verbs(self):
        """Il conto su tutto l'albero, non l'ispezione di un punto: si ripulisce
        il chiamante del ticket e quello accanto continua a chiamare."""
        colpevoli = []
        for f in _sorgenti():
            testo = f.read_text(errors="ignore").lower()
            for residuo in RESIDUI:
                if residuo in testo:
                    colpevoli.append(f"{f.relative_to(RADICE.parent)}:{residuo}")
        self.assertEqual(colpevoli, [], f"residui: {colpevoli}")

    def test_no_proxy_to_the_removed_gateway_route(self):
        """La rotta interna del gateway non esiste più: tenere in piedi il proxy
        che la chiama significa esporre alla WebUI un endpoint che può solo
        rispondere 502."""
        rotta = "/api/topics/{tier}/{name}/telegram"
        testo = (RADICE / "api" / "topics.py").read_text(errors="ignore")
        self.assertNotIn(rotta, testo, "proxy verso una rotta che non esiste più")
        client = (RADICE / "api" / "topics_client.py").read_text(errors="ignore")
        for morto in ("def telegram_binding(", "async_telegram_binding"):
            with self.subTest(morto):
                self.assertNotIn(morto, client)

    def test_the_ingress_side_is_untouched(self):
        """La controprova, perché questa pulizia ha un modo ovvio di sbagliare:
        il trasporto inbound — `telegram-bindings.json` e il relay che lo legge —
        deve restare in piedi."""
        self.assertTrue((RADICE / "api" / "telegram_bindings_client.py").exists())
        relay = (RADICE / "api" / "channel_relay.py").read_text(errors="ignore")
        self.assertIn("telegram_bindings_client", relay)


if __name__ == "__main__":
    unittest.main()
