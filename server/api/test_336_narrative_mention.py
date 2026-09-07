"""clodia-logic#336 · un resoconto non è una convocazione.

Il sintomo raccolto in `#risoluzione-issue-clodia`, e ripetuto da più bot:

    @router: @fullstack-dev è stato taggato da sysadmin, ma la catena di delega
    ha raggiunto il limite di 5 passaggi: nessun turno è partito.

Quel `@fullstack-dev` non chiedeva niente a nessuno: era il soggetto di un
racconto su un fatto già avvenuto. Il parser fa il suo dovere (R12: qualunque
`@nome` fuori da una riga citata è una convocazione), ma la convocazione
involontaria costa un turno pieno, e insieme a una richiesta VERA nello stesso
messaggio produce due menzioni — cioè la domanda di disambiguazione, e in coda a
una catena lunga il vicolo cieco di #332.

La regola che questi controlli fissano non ha potere di veto: il riconoscimento
del resoconto serve solo a SCIOGLIERE un'ambiguità, mai a togliere l'ultimo
bersaglio. Quindi:

· due menzioni di cui una sola vera → parte la vera, senza domanda;
· una menzione narrativa DA SOLA → il turno parte comunque, e resta una riga
  d'audit: un falso negativo del riconoscimento degrada al comportamento di
  oggi, non a un turno che non parte in silenzio;
· due menzioni vere, o due narrative → nulla cambia, la domanda di R3 resta.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_r3_one_mention import _Base

_PARTECIPANTI = ["owner", "clodia", "worker", "accountant"]


class _Racconto(_Base):
    """Delega da un agente, con lo stesso montaggio di `AnAgentIsAskedBackTests`."""

    async def _delegate(self, text: str, from_agent: str = "clodia"):
        start = AsyncMock(return_value=True)
        posts, apri = self._channel(_PARTECIPANTI)
        with apri(patch.object(channels, "_start_turn", start)):
            await channels._maybe_delegate("P0", "ops", from_agent, text, "owner", 0)
        return posts, start

    def _avviato(self, start) -> str | None:
        return start.await_args.args[3].name if start.await_count else None


class ANarrativeMentionLosesToTheRealRequestTests(_Racconto):

    async def test_the_report_does_not_make_the_request_ambiguous(self) -> None:
        """Il caso della issue, parola per parola: il resoconto di una menzione
        altrui accanto alla richiesta vera. Oggi sono due menzioni, quindi una
        domanda e nessun turno; il destinatario vero c'era, ed era uno solo."""
        posts, start = await self._delegate(
            "@worker è stato taggato da sysadmin, ma la catena di delega era al "
            "limite: nessun turno è partito. @accountant riprendi tu il lavoro.")

        self.assertEqual("accountant", self._avviato(start),
                         "il turno doveva partire per la richiesta vera")
        self.assertEqual([], [p for p in posts if p["author"] == "router"],
                         "nessuna domanda: l'ambiguità era solo apparente")

    async def test_an_attribution_is_not_a_summons(self) -> None:
        """«approvato da @clodia» attribuisce un atto passato, non ne chiede uno.
        Il confine è il participio: «fatti aiutare da @clodia» non è un
        resoconto, e non deve entrare in questo ramo (test sotto)."""
        _posts, start = await self._delegate(
            "Il piano approvato da @clodia lo implemento io. @worker fai la review.",
            from_agent="accountant")

        self.assertEqual("worker", self._avviato(start))

    async def test_an_infinitive_is_not_a_report(self) -> None:
        """Il controesempio che tiene onesta la regola: qui `@clodia` è il
        destinatario di una richiesta, scritta con la stessa preposizione. Due
        richieste vere → la domanda resta, che è il comportamento di R3."""
        _posts, start = await self._delegate(
            "@worker fatti aiutare da @clodia sulla stima.", from_agent="accountant")

        self.assertEqual("accountant", self._avviato(start),
                         "la domanda deve tornare all'autore: due richieste vere")
        self.assertEqual("disambigua", start.await_args.args[6])

    async def test_a_narrated_agent_asked_in_the_same_message_still_wins(self) -> None:
        """Chi viene prima raccontato e poi chiamato è chiamato: si guardano
        TUTTE le frasi in cui la menzione compare, non la prima. Altrimenti un
        resoconto in apertura disinnescherebbe la richiesta in chiusura."""
        _posts, start = await self._delegate(
            "@worker ha già risposto ieri. Ora @accountant ha chiuso i conti, "
            "quindi @worker prosegui tu.")

        self.assertEqual("worker", self._avviato(start))


class ALoneNarrativeMentionStillStartsATurnTests(_Racconto):

    async def test_the_last_target_is_never_taken_away(self) -> None:
        """Nessun potere di veto: se la menzione narrativa è l'unica, il turno
        parte come sempre. Declassarla qui vorrebbe dire lo stesso guasto di
        #332 — nessun turno e nessun segnale — su un percorso nuovo."""
        _posts, start = await self._delegate(
            "@worker è stato taggato ieri e non ha ancora risposto.")

        self.assertEqual("worker", self._avviato(start))

    async def test_but_it_leaves_a_line_to_audit(self) -> None:
        """Il «log di warning distinto per audit» della issue: è ciò che permette
        di contare il pattern invece di dedurlo dai canali."""
        with self.assertLogs(channels.LOG, level="WARNING") as log:
            await self._delegate("@worker è stato taggato ieri e non ha risposto.")

        righe = [r for r in log.output if "narrativa" in r]
        self.assertEqual(1, len(righe), f"nessuna riga d'audit fra {log.output}")
        self.assertIn("worker", righe[0])


class TheQuestionSurvivesWhereItIsStillNeededTests(_Racconto):

    async def test_two_real_requests_are_still_ambiguous(self) -> None:
        """Non-regressione di R3: il riconoscimento non deve diventare un modo
        per scegliere il primo tag in silenzio."""
        _posts, start = await self._delegate(
            "@worker prepara il diff, @accountant controlla i numeri.")

        self.assertEqual("clodia", self._avviato(start))
        self.assertEqual("disambigua", start.await_args.args[6])

    async def test_two_narrative_mentions_do_not_pick_a_winner(self) -> None:
        """Se non resta esattamente UNA menzione vera, non si indovina: l'autore
        è l'unico che sa cosa intendeva, e la domanda è già il rimedio previsto."""
        _posts, start = await self._delegate(
            "@worker è stato taggato da me e @accountant ha già risposto.")

        self.assertEqual("clodia", self._avviato(start))
        self.assertEqual("disambigua", start.await_args.args[6])


if __name__ == "__main__":
    unittest.main()
