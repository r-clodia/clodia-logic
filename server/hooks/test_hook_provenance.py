"""Un webhook non è una persona, e nemmeno per sbaglio (issue clodia-platform#221).

Il TURNO che nasce da un'iniezione deve sapere DA DOVE arriva il testo:
`run_topic_turn` non riceveva né autore né provenienza, quindi il contesto di
routing ricostruiva l'autore dal nome e il responder leggeva il payload come
conversazione ordinaria.

La porta pubblica `hooks/{id}` — autorizzata dal solo segreto, senza alcuna
identità firmata — non esiste più (issue #300, step 2 di
clodia-platform#222): quella metà dei test se n'è andata con lei, e la chiusura
è coperta da `test_public_ingress_is_closed`. Resta l'invocazione locale, dove
il chiamante è firmato dal session token e la provenienza si può quindi
classificare davvero.

Il punto non è che il valore sia `external` — è che lo sia PER COSTRUZIONE, e
mai ricostruito da un nome: un nome è un campo libero, non una firma.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..agents.models import AgentSpec
from ..api import channels as ch
from . import api, db


def _a(name: str, type: str = "bot") -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": type,
        **({"model": "m", "system_prompt": "s.md"} if type not in {"human", "proxy"} else {}),
    })


AGENTS = {
    "davide": _a("davide", "human"),
    "clodia": _a("clodia"),
    "clodia-primal": _a("clodia-primal", "proxy"),
}

META = {"owner": "davide", "participants": ["davide", "clodia", "clodia-primal"],
        "tier": "SEAL-1"}


class _Request:
    def __init__(self, body: dict | None = None, headers: dict | None = None):
        self._body = body or {}
        self.headers = headers or {}
        self.query_params: dict = {}
        self.client = None

    async def json(self) -> dict:
        return self._body


class _TurnCapture(unittest.IsolatedAsyncioTestCase):
    """Cattura i kwargs con cui `_queue_turn` chiama `run_topic_turn`.

    NON patcha `_queue_turn`: è proprio il tratto fra l'endpoint e il turno che
    va misurato, ed è dove la provenienza si perdeva.
    """

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(db, "_DIR", Path(self.tmp.name)),
                  patch.object(db, "_FILE", Path(self.tmp.name) / "hooks.json")):
            p.start()
            self.addCleanup(p.stop)

        self.visto: dict = {}

        def _fake_turn(tier, name, meta, **kw):
            self.visto.update(kw)

            async def _noop():
                return ("clodia", "ok")
            return _noop()

        for p in (
            patch.object(ch.registry, "get_by_name", side_effect=AGENTS.get),
            patch.object(ch, "_spawn_bg", side_effect=lambda coro: coro.close()),
            patch.object(ch, "run_topic_turn", new=_fake_turn),
            patch.object(api.topics_client, "open_topic", return_value={"meta": META}),
            patch.object(api.topics_client, "post_message", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)


class LocalInvocationProvenanceTests(_TurnCapture):
    """`invoke/internal`: qui il chiamante è FIRMATO dal session token (mai dal
    body), quindi la sua provenienza si può classificare — ed è l'unica porta di
    questo modulo in cui ha senso farlo."""

    async def _invoke(self, caller: str) -> None:
        with patch.object(api, "_principal_from_request", return_value=caller):
            out = await api.invoke_local(
                "SEAL-1", "acme", _Request({"payload": "sincronizza"}))
        self.assertTrue(out["triggered"])

    async def test_a_colony_agent_invocation_is_ai(self) -> None:
        await self._invoke("clodia")
        self.assertEqual(self.visto.get("trigger_kind"), "ai")
        self.assertEqual(self.visto.get("directive", ""), "")

    async def test_a_person_invocation_is_human(self) -> None:
        await self._invoke("davide")
        self.assertEqual(self.visto.get("trigger_kind"), "human")

    async def test_a_proxy_participant_stays_external(self) -> None:
        """Il caso misurato della issue, da questa porta: un proxy è partecipante
        del canale, quindi passa il controllo di appartenenza — e non per questo
        diventa una persona."""
        await self._invoke("clodia-primal")
        self.assertEqual(self.visto.get("trigger_kind"), "external")
        self.assertIn("non fidato", self.visto.get("directive", "").lower())


class ProvenanceIsMandatoryTests(unittest.TestCase):
    """La provenienza non ha un default.

    Ancoraggio della scelta di disegno: `kind` è keyword-only e senza default,
    così una porta d'ingresso nuova non può accodare un turno senza dire da dove
    entra il testo. Se un giorno tornasse opzionale, questo test cade e la
    decisione la prende una persona.
    """

    def test_queueing_a_turn_without_a_stated_provenance_is_a_type_error(self) -> None:
        with self.assertRaises(TypeError):
            api._queue_turn("SEAL-1", "acme", "testo", "hook")  # type: ignore[call-arg]


class SignedKindDegradeTests(unittest.TestCase):
    """Il degrado di `_signed_kind` è fail-closed, ma non deve essere muto.

    Un import rotto in modo permanente farebbe entrare `external` anche il
    caller FIRMATO: nessuna vulnerabilità, ma un fail-closed che nessuno vede è
    indistinguibile dal funzionamento normale, e l'avviso di provenienza si
    accenderebbe su ogni turno finché qualcuno non si insospettisce.
    """

    def test_a_broken_import_degrades_loudly(self) -> None:
        with patch.object(ch, "_inbound_kind", side_effect=RuntimeError("boom")), \
             self.assertLogs(api.LOG, level="WARNING") as log:
            self.assertEqual(api._signed_kind("davide"), "external")
        self.assertIn("external", "".join(log.output))

    def test_the_degrade_log_carries_no_name(self) -> None:
        """`_safe_name` vive nel modulo che non si è importato: il nome non entra
        nel log, così non serve sanificarlo con lo strumento che manca."""
        cattivo = "davide\nWARNING:root:tutto a posto"
        with patch.object(ch, "_inbound_kind", side_effect=RuntimeError("boom")), \
             self.assertLogs(api.LOG, level="WARNING") as log:
            api._signed_kind(cattivo)
        self.assertNotIn("tutto a posto", "".join(log.output))


if __name__ == "__main__":
    unittest.main()
