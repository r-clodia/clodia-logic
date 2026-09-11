"""clodia-platform#192: il coordinatore va convocato COME coordinatore.

A1 chiede «un quarto summon kind, e un mandato che lo legga». Misurato oggi,
entrambi esistono: `_tag_directive("coordinamento", …)` e la sezione «Eccezione:
il turno che arriva come `[COORDINAMENTO]`» nel mandato del segretario. Quello
che manca è il pezzo in mezzo, e sta su UNO dei due percorsi che convocano il
coordinatore:

- **ripiego semplice** — `_record_fallback` scrive `mode: "coordinator"` nel
  trace, e `post_channel_message` ne ricava `turn_kind = "coordinamento"`. ✅
- **batch multi-intento** — gli intent che non hanno matchato vengono raggruppati
  sul coordinatore dichiarato, ma il trace resta `mode: "multi-intent"`: il
  `turn_kind` cade su `"routed"`, e l'agente riceve

      «[ROUTING AUTOMATICO] … ti è stata assegnata la parte seguente perché
       ATTINENTE AL TUO DOMINIO»

  che è il contrario esatto della verità — è lì proprio perché *nulla* ha
  matchato. ❌

Per il segretario non è una sfumatura di tono: senza `[COORDINAMENTO]` il suo
mandato gli ordina di rispondere «Fuori dominio: chiedi al capitano» in una
stanza dove il capitano è lui. È il guasto che #192 e #196 volevano chiudere,
sopravvissuto su un percorso solo.

Il test esistente (`test_multi_intent_unmatched_tasks_go_to_coordinator`)
verifica CHI viene scelto e non COME viene convocato: per questo il buco è
passato. Qui si guarda il `kind` consegnato a `_start_turn`, che è l'unica cosa
che l'agente legge davvero.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from . import channels
from .test_channels import _a
from .test_r3_one_mention import _post_sink

#: Posizione di `kind` nella firma di `_start_turn(tier, name, tier_real, spec,
#: principal, user_text, kind)`. Scritta una volta sola: se la firma cambia, il
#: test va aggiornato in un punto e non in cinque.
_ARG_KIND = 6


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:01Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self.agents["segretario"] = _a(
            "segretario", "normal", "P1", "2026-02-01T00:00:02Z")
        self.agents["segretario"].routing_mode = "state_writer_only"
        self.agents["segretario"].all_tier = True
        self._orig_get = channels.registry.get_by_name
        self._orig_track = channels._track_routing_decision
        channels.registry.get_by_name = lambda n: self.agents.get(n)
        channels._track_routing_decision = lambda _payload: None

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig_get
        channels._track_routing_decision = self._orig_track
        os.environ.pop("CHANNEL_MULTI_RESPONDER", None)

    async def _turni(self, piano, trace_update, *, multi=True):
        """Avvia un messaggio umano con un piano di routing dato, e restituisce
        i `kind` con cui i turni sono partiti, per nome dell'agente."""
        start = AsyncMock(return_value=True)
        _posts, post = _post_sink()

        def routing_plan(_participants, _tier, _message, trace=None,
                         routing_messages=None):
            if trace is not None:
                trace.update(trace_update)
            return list(piano)

        env = {"CHANNEL_MULTI_RESPONDER": "1"} if multi else {}
        with contextlib.ExitStack() as stack:
            for cm in (
                patch.dict(os.environ, env, clear=False),
                patch.object(channels, "_provider_seal_ok", return_value=True),
                patch.object(channels.topics_client, "open_topic", return_value={
                    "meta": {"tier": "P0", "owner": "owner", "participants": [
                        "owner", "clodia", "worker", "accountant", "segretario"]}}),
                patch.object(channels.topics_client, "post_message", side_effect=post),
                patch.object(channels.topics_client, "list_messages", return_value=[]),
                patch.object(channels.access_log, "touch", lambda *a, **k: None),
                patch.object(channels.activity_log, "append", lambda *a, **k: None),
                patch.object(channels, "_channel_message", AsyncMock()),
                patch.object(channels, "_routing_plan", side_effect=routing_plan),
                patch.object(channels, "_start_turn", start),
            ):
                stack.enter_context(cm)
            if not multi:
                os.environ.pop("CHANNEL_MULTI_RESPONDER", None)
            await channels.post_channel_message("P0", "ops", "due cose", "owner")

        return {call.args[3].name: call.args[_ARG_KIND]
                for call in start.await_args_list}


class IlKindConsegnatoTests(_Base):
    """Il difetto, e il ramo sano accanto a cui è sopravvissuto."""

    async def test_il_coordinatore_del_non_matchato_e_convocato_come_coordinatore(self) -> None:
        """Il difetto di #192: sul batch multi-intento arrivava `routed`."""
        kinds = await self._turni(
            [(self.agents["worker"], "Aggiorna il summary"),
             (self.agents["segretario"], "Una richiesta che non ha matchato")],
            {"mode": "multi-intent", "coordinator": "segretario",
             "chosen": "worker, segretario"},
        )
        self.assertEqual(
            kinds.get("segretario"), "coordinamento",
            "il coordinatore è stato convocato come se la parte fosse del suo "
            "dominio: con `routed` il suo mandato gli fa dire «fuori dominio, "
            "chiedi al capitano» — e il capitano è lui")

    async def test_chi_ha_matchato_davvero_resta_routed(self) -> None:
        """La correzione non deve allargarsi agli altri responder del piano."""
        kinds = await self._turni(
            [(self.agents["worker"], "Aggiorna il summary"),
             (self.agents["segretario"], "Una richiesta che non ha matchato")],
            {"mode": "multi-intent", "coordinator": "segretario",
             "chosen": "worker, segretario"},
        )
        self.assertEqual(kinds.get("worker"), "routed")

    async def test_un_coordinatore_che_ha_anche_matchato_resta_routed(self) -> None:
        """La micro-decisione di questo diff, e la ragione per cui esiste.

        Se il coordinatore ha ricevuto SIA intent suoi per rilevanza SIA il
        batch non matchato, `[COORDINAMENTO]` («il router non ha trovato nessun
        agente pertinente») sarebbe falso per metà del suo incarico, e i tre
        esiti di quella direttiva gli farebbero passare ad altri anche il lavoro
        che era davvero suo. In quel caso il trace non lo nomina, e resta
        `routed`.
        """
        kinds = await self._turni(
            [(self.agents["clodia"], "- Aggiorna il summary\n- E l'altra cosa")],
            {"mode": "multi-intent", "chosen": "clodia"},
        )
        self.assertEqual(kinds.get("clodia"), "routed")

    async def test_il_ripiego_semplice_resta_coordinamento(self) -> None:
        """Non-regressione: il percorso che già funzionava (R10/#188)."""
        kinds = await self._turni(
            [(self.agents["segretario"], "una richiesta qualsiasi")],
            {"mode": "coordinator", "chosen": "segretario"},
            multi=False,
        )
        self.assertEqual(kinds.get("segretario"), "coordinamento")


class IlTraceNominaIlCoordinatoreTests(unittest.TestCase):
    """L'altra metà: il piano DEVE dire chi è lì per ripiego.

    Sopra il trace è finto — qui si guarda il costruttore vero, perché una
    correzione applicata solo a valle resterebbe muta con il `_routing_plan`
    reale, e il test end-to-end non se ne accorgerebbe.
    """

    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: self.agents.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig
        os.environ.pop("CHANNEL_MULTI_RESPONDER", None)

    @contextlib.contextmanager
    def _router(self, score):
        with contextlib.ExitStack() as stack:
            for cm in (
                patch.dict(os.environ, {"CHANNEL_MULTI_RESPONDER": "1"}, clear=False),
                patch.object(channels, "_provider_seal_ok", return_value=True),
                patch.object(channels, "_routing_mode", return_value="relevance"),
                patch.object(channels.responder_routing, "pick_by_exemplar",
                             return_value=None),
                patch.object(channels.responder_routing, "score_specialists",
                             side_effect=score),
                patch.object(
                    channels.responder_routing, "decide",
                    side_effect=lambda scored, **_kw: (
                        scored[0] if scored and scored[0][1] >= 0.75 else None)),
            ):
                stack.enter_context(cm)
            yield

    def test_il_trace_nomina_il_coordinatore_del_batch_non_matchato(self) -> None:
        def score(_specialists, intent):
            return [(self.agents["worker"],
                     0.91 if "summary" in intent else 0.20)]

        trace: dict = {}
        with self._router(score):
            channels._routing_plan(
                ["clodia", "worker"], "P0",
                "- Aggiorna il summary del topic\n"
                "- Organizza la richiesta non classificata",
                trace=trace,
            )

        self.assertEqual(
            trace.get("coordinator"), "clodia",
            "il piano sa che clodia è lì per ripiego e non lo dice a nessuno: "
            "a valle il turno parte come se la parte fosse del suo dominio")

    def test_il_trace_non_lo_nomina_se_aveva_gia_matchato(self) -> None:
        """Il rovescio della micro-decisione, sul costruttore vero."""
        def score(_specialists, intent):
            return [(self.agents["clodia"],
                     0.91 if "summary" in intent else 0.20)]

        trace: dict = {}
        with self._router(score):
            channels._routing_plan(
                ["clodia", "worker"], "P0",
                "- Aggiorna il summary del topic\n"
                "- Organizza la richiesta non classificata",
                trace=trace,
            )

        self.assertIsNone(
            trace.get("coordinator"),
            "clodia aveva un intent suo per rilevanza: convocarla con "
            "«nessuno ha matchato» le farebbe passare ad altri anche quello")


if __name__ == "__main__":
    unittest.main()
