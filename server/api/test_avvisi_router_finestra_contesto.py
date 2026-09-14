"""Un avviso di servizio del router non sfratta la conversazione dal prompt.

Osservato in `SEAL-1/software-house` il 14/09/2026, e documentato in
`local/dossier-misinstradamento-redelivery-20260913.md` (occorrenze 5 e 6).

## Il sintomo

`$clodia` tagga `@davide`, che è l'owner del topic ma **non** un participant. Il
router fa il suo dovere e posta l'avviso di non-recapito. Poi qualcuno ritagga, e
l'avviso ricompare identico. E ancora. Nel prompt ricevuto da `fullstack-dev-181`
gli avvisi erano **dieci su quindici righe** di storico — otto su `@davide`, due
nella variante che cita anche `@security-engineer`.

## Perché è un difetto e non rumore

Due effetti distinti, entrambi misurati, ed è la ragione per cui i fix sono due.

**1. Lo sfratto.** `_history_prompt` monta una finestra di 15 righe, che è tutto
il contesto che un turno riceve. Dieci righe di avvisi identici ne lasciano
cinque alla conversazione vera: l'assegnazione di cui il turno doveva occuparsi
era già scorsa fuori. Il costo non è estetico — è che un messaggio di servizio,
che per definizione non porta lavoro, **espelle il lavoro** dal solo posto in cui
l'agente può vederlo. E peggiora proprio mentre il ciclo va avanti, cioè quando
il contesto servirebbe di più.

**2. Il turno puntato sull'avviso.** In `_reused_turn_prompt`, il `router` veniva
contato fra i «terzi» di cui il responder deve recuperare il filo. Un solo avviso
comparso dopo il suo ultimo intervento bastava quindi a sostituire il `fallback`
— il messaggio a cui rispondere davvero — con lo storico intero, che si chiude
con «Rispondi all'ultimo messaggio». E l'ultimo messaggio era l'avviso. Il turno
veniva puntato su una bolla a cui non c'è niente da rispondere: ciò che manca
(l'invito, o la decisione dell'owner) è fuori dalla stanza per costruzione.

## Cosa NON fa questo fix, e va detto

Non impedisce al turno di *partire*. Chi decide l'allocazione sta altrove; qui si
decide soltanto **cosa vede** il turno una volta partito. È la metà del problema
che vive in questo modulo, ed è quella che costava contesto a ogni agente del
canale, non solo a chi serviva l'avviso.
"""
from __future__ import annotations

import unittest

from . import channels


def _avviso(nome: str = "davide") -> dict:
    return {
        "author": channels._ROUTING_DIALOG_AUTHOR,
        "kind": "system",
        "text": (f"@{nome} è stato taggato da clodia, ma non partecipa a questo "
                 f"canale: nessun turno è partito, e la catena si ferma qui.\n\n"
                 f"<!-- invite={nome} -->"),
    }


def _msg(autore: str, testo: str) -> dict:
    return {"author": autore, "kind": "ai", "text": testo}


class AServiceNoticeDoesNotEvictTheConversationTests(unittest.TestCase):
    """Il fix 1: la finestra di 15 righe non si riempie di avvisi identici."""

    def test_ripetizioni_identiche_collassano_sull_ultima(self):
        storia = [_avviso() for _ in range(10)]
        tenuti = channels._collassa_avvisi_router(storia)
        self.assertEqual(1, len(tenuti),
                         "dieci avvisi identici informano quanto uno solo")
        self.assertIs(storia[-1], tenuti[0],
                      "sopravvive l'occorrenza più recente, non la prima")

    def test_l_ordine_di_conversazione_e_preservato(self):
        a, b = _msg("clodia", "ti assegno #419"), _msg("fullstack-dev", "preso")
        tenuti = channels._collassa_avvisi_router([a, _avviso(), b, _avviso()])
        self.assertEqual([a, b, _avviso()], tenuti[:2] + [tenuti[2]],
                         "gli altri messaggi restano dove stavano")

    def test_avvisi_su_bersagli_diversi_restano_tutti(self):
        d, s = _avviso("davide"), _avviso("security-engineer")
        self.assertEqual([d, s], channels._collassa_avvisi_router([d, s]),
                         "sono fatti diversi: nessuno dei due è ridondante")

    def test_l_assegnazione_non_esce_dalla_finestra(self):
        """Il caso reale: la riga che conta è a 16 messaggi dal fondo.

        Senza il collasso resta fuori dalle ultime 15 e il turno non la vede —
        che è esattamente come si sono persi tre turni su #416.
        """
        storia = ([_msg("clodia", "ti assegno #419 + #405")]
                  + [_avviso() for _ in range(10)]
                  + [_msg("clodia", f"nota {i}") for i in range(5)])
        prompt = channels._history_prompt("software-house", "SEAL-1", storia)
        self.assertIn("ti assegno #419 + #405", prompt)
        self.assertEqual(1, prompt.count("non partecipa a questo canale"),
                         "l'avviso resta, una volta: informa senza sfrattare")


class ARouterNoticeIsNotAThirdPartyTests(unittest.TestCase):
    """Il fix 2: un avviso da solo non promuove il turno a storico intero."""

    def _prompt(self, storia: list[dict]) -> str:
        orig = channels.topics_client.list_messages
        channels.topics_client.list_messages = lambda *a, **k: storia
        try:
            return channels._reused_turn_prompt(
                "SEAL-1", "software-house", "fullstack-dev-181", "clodia",
                "FALLBACK")
        finally:
            channels.topics_client.list_messages = orig

    def test_solo_avvisi_non_visti_lasciano_il_fallback(self):
        storia = [_msg("fullstack-dev-181", "fatto"), _avviso(), _avviso()]
        self.assertEqual("FALLBACK", self._prompt(storia),
                         "il router non è un terzo con cui recuperare il filo")

    def test_un_terzo_vero_porta_ancora_lo_storico(self):
        """Il confine: la regressione da non introdurre è l'opposta."""
        storia = [_msg("fullstack-dev-181", "fatto"), _avviso(),
                  _msg("sysadmin", "ho aperto la issue")]
        self.assertIn("ho aperto la issue", self._prompt(storia))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
