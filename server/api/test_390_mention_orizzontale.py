"""Modello nave (clodia-platform#390): una menzione ORIZZONTALE non apre turni.

clodia-logic#458 ha reso il coordinatore l'unico destinatario dei messaggi umani
non indirizzati, ma il divieto di delega fra specialisti restava affidato alla
sola costituzione: `_maybe_delegate` apriva un turno per QUALUNQUE `@tag` di un
agente, senza guardare chi lo aveva scritto. Un modello che non segue il
principio 4 — o un pack con una costituzione vecchia — rimetteva in piedi le
catene specialista→specialista che il modello nave vieta.

La regola che questi test fissano è STRUTTURALE, sul bersaglio della menzione,
non un filtro sul testo:

- chi non è coordinatore dichiarato (`agents.coordinator.DECLARED`) apre turni
  solo verso un coordinatore dichiarato;
- gli altri `@` restano menzioni — compaiono nel testo, non convocano nessuno —
  e lasciano una traccia (`horizontal-mention-blocked`) perché la deriva si
  veda invece di essere dedotta dal silenzio;
- il coordinatore continua a delegare come prima: è il suo mestiere.

Eccezione unica e voluta: il guardiano in modalità debug (`@sysadmin`), che
esiste proprio per i casi in cui la catena ordinaria è rotta. Il Messaggero NON
è eccezione — decisione esplicita sulla issue: uno specialista senza grant di
comunicazione riferisce a Clodia/Segretario, non convoca il messaggero.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a


class _Base(unittest.IsolatedAsyncioTestCase):
    PARTECIPANTI = ["owner", "clodia", "segretario", "worker", "accountant",
                    "messaggero"]

    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "segretario": _a("segretario", "normal", "P3", "2026-01-01T00:00:01Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:01Z"),
            "messaggero": _a("messaggero", "normal", "P1", "2026-02-01T00:00:02Z"),
            "sysadmin": _a("sysadmin", "normal", "P3", "2026-02-01T00:00:03Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig_get = channels.registry.get_by_name
        self._orig_track = channels._track_routing_decision
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        channels._track_routing_decision = lambda _payload: None
        os.environ.pop("CHANNEL_MULTI_RESPONDER", None)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._track_routing_decision = self._orig_track

    async def _delegate(self, from_agent: str, text: str, participants=None,
                        debug: bool = False):
        """Esegue `_maybe_delegate` e ritorna (turni avviati, post, eventi)."""
        start = AsyncMock(return_value=True)
        posts: list[dict] = []
        eventi: list = []

        def post(_tier, _name, author, testo, kind="human", **_kw):
            row = {"id": str(len(posts) + 1), "author": author, "text": testo,
                   "kind": kind}
            posts.append(row)
            return row

        async def publish(event):
            eventi.append(event)

        patches = [
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels.topics_client, "open_topic", return_value={
                "meta": {"tier": "P0",
                         "participants": participants or self.PARTECIPANTI}}),
            patch.object(channels.topics_client, "post_message", side_effect=post),
            patch.object(channels.topics_client, "list_messages", return_value=[]),
            patch.object(channels.access_log, "touch", lambda *a, **k: None),
            patch.object(channels.activity_log, "append", lambda *a, **k: None),
            patch.object(channels, "_channel_message", AsyncMock()),
            patch.object(channels, "_start_turn", start),
            patch.object(channels.bus, "publish", publish),
            patch.object(channels.debug_watch, "enabled", return_value=debug),
        ]
        with contextlib.ExitStack() as stack:
            for cm in patches:
                stack.enter_context(cm)
            await channels._maybe_delegate("P0", "ops", from_agent, text,
                                           "owner", 0)
        avviati = [c.args[3].name for c in start.await_args_list]
        return avviati, posts, eventi

    @staticmethod
    def _bloccati(eventi) -> list:
        return [e for e in eventi
                if (e.payload or {}).get("mode") == "horizontal-mention-blocked"]


class UnoSpecialistaNonSvegliaUnAltroTests(_Base):
    """Il caso della issue: la catena orizzontale si ferma prima di partire."""

    async def test_lo_specialista_taggato_non_riceve_nessun_turno(self) -> None:
        avviati, _posts, _eventi = await self._delegate(
            "worker", "@accountant ci pensi tu ai conti?")
        self.assertEqual([], avviati)

    async def test_il_blocco_lascia_una_traccia_col_nome_del_bersaglio(self) -> None:
        """Senza traccia la deriva si vedrebbe solo come un turno che non parte,
        cioè esattamente come un guasto."""
        _avviati, _posts, eventi = await self._delegate(
            "worker", "@accountant ci pensi tu ai conti?")
        bloccati = self._bloccati(eventi)
        self.assertEqual(1, len(bloccati))
        self.assertEqual(["accountant"], bloccati[0].payload["negati"])
        self.assertEqual("worker", bloccati[0].payload["from_agent"])

    async def test_il_canale_non_si_riempie_di_avvisi(self) -> None:
        """La menzione resta nel testo dell'autore: un secondo messaggio di
        sistema per ogni `@` sarebbe rumore su un canale che già lo legge."""
        _avviati, posts, _eventi = await self._delegate(
            "worker", "@accountant ci pensi tu ai conti?")
        self.assertEqual([], posts)

    async def test_nemmeno_il_messaggero_e_un_eccezione(self) -> None:
        """Decisione esplicita di #390: chi non ha il grant per comunicare
        riferisce a Clodia/Segretario, non convoca il messaggero."""
        avviati, _posts, eventi = await self._delegate(
            "worker", "@messaggero manda questa mail")
        self.assertEqual([], avviati)
        self.assertEqual(["messaggero"], self._bloccati(eventi)[0].payload["negati"])


class VersoIlCoordinatoreSiPassaTests(_Base):
    """Il resoconto verso l'alto è la via prevista dal principio 4: resta aperta."""

    async def test_uno_specialista_sveglia_clodia(self) -> None:
        avviati, _posts, eventi = await self._delegate(
            "worker", "@clodia ho finito, ecco l'esito")
        self.assertEqual(["clodia"], avviati)
        self.assertEqual([], self._bloccati(eventi))

    async def test_uno_specialista_sveglia_il_segretario(self) -> None:
        avviati, _posts, _eventi = await self._delegate(
            "worker", "@segretario sono bloccato, serve una decisione")
        self.assertEqual(["segretario"], avviati)

    async def test_la_menzione_orizzontale_non_rende_ambigua_la_richiesta(self) -> None:
        """«@clodia, per la parte fiscale servirebbe @accountant» è UNA richiesta
        sola: se il filtro girasse dopo R3, l'autore si vedrebbe chiedere «chi
        intendevi attivare?» su un nome che comunque non poteva convocare."""
        avviati, posts, _eventi = await self._delegate(
            "worker", "@clodia la parte fiscale la vedrebbe @accountant")
        self.assertEqual(["clodia"], avviati)
        self.assertEqual([], [p for p in posts if p["author"] == "router"])


class IlCoordinatoreDelegaComePrimaTests(_Base):
    """Nessuna regressione sul percorso che il modello nave prevede."""

    async def test_clodia_assegna_a_uno_specialista(self) -> None:
        avviati, _posts, eventi = await self._delegate(
            "clodia", "@worker prendi tu questa")
        self.assertEqual(["worker"], avviati)
        self.assertEqual([], self._bloccati(eventi))

    async def test_il_segretario_assegna_a_uno_specialista(self) -> None:
        avviati, _posts, _eventi = await self._delegate(
            "segretario", "@accountant a te")
        self.assertEqual(["accountant"], avviati)

    async def test_vale_il_SEED_non_l_etichetta_dell_istanza(self) -> None:
        """`clodia-2` è sempre il coordinatore: il confronto è per seed, come
        ovunque in questo modulo (issue#94)."""
        avviati, _posts, _eventi = await self._delegate(
            "clodia-2", "@worker prendi tu questa")
        self.assertEqual(["worker"], avviati)


class IlGuardianoRestaRaggiungibileTests(_Base):
    """L'unica eccezione: in debug `@sysadmin` risponde anche a uno specialista,
    perché è la via d'uscita di chi è bloccato da un guasto — e un guasto è
    proprio il caso in cui la catena verso il coordinatore può non bastare."""

    async def test_in_debug_uno_specialista_sveglia_il_guardiano_partecipante(self) -> None:
        avviati, _posts, _eventi = await self._delegate(
            "worker", "@sysadmin qui qualcosa non gira",
            participants=self.PARTECIPANTI + ["sysadmin"], debug=True)
        self.assertEqual(["sysadmin"], avviati)

    async def test_fuori_dal_debug_il_guardiano_non_e_una_scorciatoia(self) -> None:
        avviati, _posts, eventi = await self._delegate(
            "worker", "@sysadmin qui qualcosa non gira",
            participants=self.PARTECIPANTI + ["sysadmin"], debug=False)
        self.assertEqual([], avviati)
        self.assertEqual(["sysadmin"], self._bloccati(eventi)[0].payload["negati"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
