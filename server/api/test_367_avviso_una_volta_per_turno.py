"""Un avviso di non-recapito informa una volta per TURNO (clodia-platform#367).

## La misura

`SEAL-1/software-house`, 14/09/2026, `agent-server.log.3` — cinque avvisi
identici in 1,1 secondi, dallo stesso spawn:

    14:55:22,851  delega da clodia-189 …: davide, security-engineer taggati ma fuori dal canale
    14:55:22,969  delega da clodia-189 …: davide taggati ma fuori dal canale
    14:55:23,192  delega da clodia-189 …: davide taggati ma fuori dal canale
    14:55:23,831  delega da clodia-189 …: davide taggati ma fuori dal canale
    14:55:23,903  delega da clodia-189 …: davide taggati ma fuori dal canale

Non sono cinque tag: è UNA risposta. Da #243 una risposta è più **bolle**, e
`_maybe_delegate` viene chiamata una volta per bolla. Il dedup per quel caso
esiste già — `serviti`, la memoria per-turno di clodia-logic#434 — ma copre solo
i bersagli **serviti**, calcolati più in basso della porta da cui esce l'avviso.
`@davide` in tre blocchi = tre avvisi.

## Perché la chiave della issue era sbagliata

La #367 propone di deduplicare per `(mittente, bersaglio, canale)`. È la chiave
sbagliata e la vita sbagliata: il mittente cambia a ogni giro (la issue lo
osserva e non se lo spiega), e una memoria che dura oltre il turno renderebbe
muto un ritag successivo, che è invece un fatto nuovo — chi ritagga domani ha
diritto di sapere che quel nome non è ancora nella stanza. La chiave giusta è il
TURNO, ed è già in casa.

## Cosa NON è la causa, verificato

L'altra metà della issue — «l'avviso apre N turni» — non ha un corrispettivo nel
codice: l'avviso è postato con `kind="system"` e autore `router` fuori da
`post_channel_message` (che è ciò che accoda i responder), `_pick_responder` con
un tag verso un non-partecipante ritorna `None`, e il gate R21 esclude dal
routing per rilevanza i messaggi non umani. Due terzi del resto erano già chiusi
il 14/09 (`_collassa_avvisi_router`, e il `router` escluso dai «terzi» in
`_reused_turn_prompt`).

Resta però vero l'effetto che la issue descrive, con un'altra causa: il turno
che parte **per altri motivi** viene PUNTATO sull'avviso, perché lo storico si
chiude con «Rispondi all'ultimo messaggio» e l'ultimo messaggio è una bolla di
servizio. Un avviso non chiede niente a nessuno: il rimedio che manca —
l'invito, o la decisione dell'owner — è fuori dalla stanza per costruzione.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from . import channels
from .test_192_handoff_fuori_stanza import _FuoriStanza


class OneNoticePerTurnTests(_FuoriStanza):
    """Le bolle di UNO stesso turno condividono la memoria: un bersaglio fuori
    stanza si annuncia una volta, come un bersaglio dentro si sveglia una volta."""

    async def _bolle(self, testi: list[str], *, serviti: set | None):
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(self.STANZA)
        with apri(patch.object(channels, "_start_turn", start),
                  patch.object(channels.bus, "publish", AsyncMock())):
            for t in testi:
                await channels._maybe_delegate(
                    "P0", "ops", "clodia", t, "owner", 0, serviti=serviti)
        return [p for p in posts
                if p["author"] == channels._ROUTING_DIALOG_AUTHOR]

    async def test_tre_bolle_stesso_bersaglio_un_avviso_solo(self) -> None:
        """Il caso misurato. Rosso prima: tre avvisi identici."""
        avvisi = await self._bolle(
            ["Questa la vede @accountant.",
             "Serve @accountant per i numeri.",
             "Riassumo: decide @accountant."],
            serviti=set())
        self.assertEqual(1, len(avvisi),
                         "la stessa risposta, spezzata in bolle, ha annunciato "
                         "più volte lo stesso mancato recapito")
        self.assertIn("accountant", avvisi[0]["text"])

    async def test_un_bersaglio_nuovo_in_una_bolla_dopo_si_annuncia(self) -> None:
        """Il confine: il dedup è per BERSAGLIO, non «un avviso per turno». Un
        secondo nome fuori stanza è un fatto nuovo e va detto, altrimenti si
        chiude il rumore nascondendo l'informazione."""
        avvisi = await self._bolle(
            ["Ci pensa @accountant.", "E anche @accountant.", "Poi c'è @reviewer."],
            serviti=set())
        self.assertEqual(2, len(avvisi))
        self.assertIn("accountant", avvisi[0]["text"])
        self.assertIn("reviewer", avvisi[1]["text"])
        self.assertNotIn("accountant", avvisi[1]["text"])

    async def test_senza_memoria_condivisa_ogni_chiamata_resta_indipendente(self) -> None:
        """`serviti=None` è il chiamante che non ha un turno da ricordare (la
        risposta non spezzata). Verde prima e dopo: questo fix non deve poter
        zittire un avviso che appartiene a un'altra risposta."""
        avvisi = await self._bolle(
            ["Ci pensa @accountant.", "Ci pensa @accountant."], serviti=None)
        self.assertEqual(2, len(avvisi))


class ATurnIsNotPointedAtAServiceNoticeTests(unittest.TestCase):
    """Lo storico si chiude con «Rispondi all'ultimo messaggio». Se l'ultimo è un
    avviso del router, quel turno nasce puntato su una bolla a cui non c'è niente
    da rispondere."""

    AVVISO = {"author": channels._ROUTING_DIALOG_AUTHOR, "kind": "system",
              "text": "@davide è stato taggato da clodia, ma non partecipa a "
                      "questo canale: nessun turno è partito."}

    def _prompt(self, messaggi):
        return channels._history_prompt("ops", "SEAL-1", messaggi)

    def test_lo_storico_normale_chiede_di_rispondere_all_ultimo(self) -> None:
        """Verde prima e dopo: il caso ordinario non cambia."""
        p = self._prompt([{"author": "davide", "kind": "human", "text": "ciao"}])
        self.assertIn("Rispondi all'ultimo messaggio", p)

    def test_se_l_ultimo_e_un_avviso_il_turno_non_ci_viene_puntato(self) -> None:
        """Rosso prima: l'istruzione finale mandava il turno a rispondere
        all'avviso."""
        p = self._prompt([
            {"author": "davide", "kind": "human", "text": "prendi la #367"},
            self.AVVISO,
        ])
        self.assertNotIn("Rispondi all'ultimo messaggio", p)
        self.assertIn("avviso di servizio", p)

    def test_l_avviso_resta_nello_storico(self) -> None:
        """Non si nasconde: chi ha taggato deve poter leggere che non è arrivato.
        Si toglie solo la pretesa che sia una richiesta."""
        p = self._prompt([{"author": "davide", "kind": "human", "text": "ciao"},
                          self.AVVISO])
        self.assertIn("davide", p)
        self.assertIn("non partecipa", p)

    def test_un_avviso_a_meta_storico_non_cambia_niente(self) -> None:
        """La regola guarda la CODA, non la presenza: un avviso vecchio seguito
        da conversazione vera è storia come tutto il resto."""
        p = self._prompt([self.AVVISO,
                          {"author": "davide", "kind": "human", "text": "allora?"}])
        self.assertIn("Rispondi all'ultimo messaggio", p)


class ASubstituteIsToldItIsOneTests(unittest.IsolatedAsyncioTestCase):
    """Occorrenza 4 del dossier: `@fullstack-dev-172` non è più vivo, la menzione
    ricade sull'allocazione normale — deliberato e giusto, la menzione non muore
    — ma chi la serve riceve «è una richiesta diretta A TE», identico a quello
    che avrebbe ricevuto il destinatario vero. La sostituzione resta in un
    `LOG.info` che l'agente non legge.

    Innocuo per una richiesta, sbagliato per un ordine con stato locale
    («continua dal tuo branch»): il sostituto quello stato non ce l'ha, e l'unico
    modo di accorgersene è andare a verificarlo — cioè il lavoro che il 14/09 si
    è ripetuto tre volte su #416.
    """

    async def _start(self, *, spawn: str | None, vivo: bool):
        chat = SimpleNamespace(chat_id="chan:SEAL-1:ch:dev",
                               _lock=SimpleNamespace(locked=lambda: False),
                               principal=None, origin=None)
        spec = SimpleNamespace(name="dev", multi_spawn=False, max_spawns=4,
                               activation="queue")
        visti: list = []

        def _riusato(tier, name, label, principal, prompt):
            visti.append(prompt)
            return prompt

        def _chiudi(coro):
            coro.close()

        with patch.object(channels.manager, "get", return_value=chat), \
             patch.object(channels, "_spawn_bg", side_effect=_chiudi), \
             patch.object(channels, "_channel_message", AsyncMock()), \
             patch.object(channels, "_annuncia_cambio_coordinatore", AsyncMock()), \
             patch.object(channels, "_provider_della_stanza_ancora_valido",
                          AsyncMock(return_value=True)), \
             patch.object(channels, "_chat_of_spawn",
                          return_value=("chan:vivo" if vivo else None)), \
             patch.object(channels, "_spawn_label", return_value="dev"), \
             patch.object(channels, "_reused_turn_prompt", side_effect=_riusato), \
             patch.object(channels, "_tag_directive", return_value="[RICHIESTA DIRETTA] …"):
            await channels._start_turn("SEAL-1", "ch", "SEAL-1", spec,
                                       "davide", "continua dal tuo branch",
                                       "direct", spawn=spawn)
        return visti[-1] if visti else ""

    async def test_chi_serve_per_ricaduta_sa_di_essere_un_sostituto(self) -> None:
        """Rosso prima: il prompt non nominava la sostituzione."""
        prompt = await self._start(spawn="dev-7", vivo=False)
        self.assertIn("dev-7", prompt)
        self.assertIn("sostituto", prompt.lower())

    async def test_il_destinatario_vero_non_riceve_nessuna_nota(self) -> None:
        """Lo spawn indirizzato è vivo: non c'è nessuna sostituzione da
        dichiarare, e una nota che compare sempre smette di essere letta."""
        prompt = await self._start(spawn="dev-7", vivo=True)
        self.assertNotIn("sostituto", prompt.lower())

    async def test_una_menzione_senza_spawn_resta_com_era(self) -> None:
        prompt = await self._start(spawn=None, vivo=False)
        self.assertNotIn("sostituto", prompt.lower())
