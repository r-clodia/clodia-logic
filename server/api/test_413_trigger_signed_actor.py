"""Su un token on-behalf, chi agisce è la PERSONA — non l'agente che lo porta.

clodia-platform#413, punto 1. La porta `trigger/internal` è l'unica che confronta
l'identità FIRMATA con un autore DICHIARATO nel body, e la leggeva con
`_principal_from_request`, cioè dal claim `agent`. Su un token on-behalf quel
claim è il **carrier** (l'agente che porta il token di una persona), mentre la
persona sta in `principal` — entrambi firmati dal runner.

Conseguenza, appena il gateway smette di buttare via il Bearer: il body dice
`davide`, la firma dice `clodia`, e la porta respinge 403 «non può innescare un
turno come 'davide'» — una persona scambiata per un impostore. È la ragione per
cui `runtime.channel_trigger` non ha mai potuto passare `auth=True`, e quindi la
ragione per cui OGNI trigger della colonia arriva anonimo e viene classificato
`external` (i log della #413: «nessun Bearer nell'header», «trigger esterno …
firmato 'sconosciuto'»).

Quella classificazione non è cosmetica: `run_topic_turn` non sceglie nessun
responder per rilevanza se `kind != "human"`, quindi un messaggio di una persona
mandato da un client MCP senza @menzione resta senza risposta — esattamente ciò
che il commento di `topic.post_message` nel gateway dichiara di aver risolto.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import agents, channels as ch


class _Req:
    """Il minimo che la porta chiede a una Request: headers + body JSON."""

    def __init__(self, body: dict, token: str | None = None) -> None:
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self._body = body

    async def json(self) -> dict:
        return self._body


class _Spec:
    def __init__(self, tipo: str) -> None:
        self.type = tipo


_CLAIMS = {
    # Token di un agente: nessun on-behalf, il carrier È l'attore.
    "tok-clodia": {"agent": "clodia"},
    # Token on-behalf: `clodia` porta il token, ma chi agisce è `davide`.
    "tok-davide": {"agent": "clodia", "principal": "davide",
                   "on_behalf": True, "human_role": "admin"},
}

_TIPI = {"clodia": "bot", "davide": "human"}


def _verifica(token: str) -> dict:
    try:
        return _CLAIMS[token]
    except KeyError:
        raise ValueError("firma non valida") from None


class SignedActorTests(unittest.TestCase):
    """La differenza fra i due lettori, isolata."""

    def setUp(self) -> None:
        from ..colony import pki
        p = patch.object(pki, "verify_session_token", side_effect=_verifica)
        p.start()
        self.addCleanup(p.stop)

    def test_an_agent_token_reads_the_same_both_ways(self) -> None:
        req = _Req({}, token="tok-clodia")
        self.assertEqual(agents._principal_from_request(req), "clodia")
        self.assertEqual(agents._signed_actor(req), "clodia")

    def test_on_behalf_the_actor_is_the_person(self) -> None:
        req = _Req({}, token="tok-davide")
        # Il claim `agent` resta il carrier: `_principal_from_request` non mente,
        # risponde a un'altra domanda.
        self.assertEqual(agents._principal_from_request(req), "clodia")
        self.assertEqual(agents._signed_actor(req), "davide")

    def test_no_bearer_is_nobody(self) -> None:
        self.assertIsNone(agents._signed_actor(_Req({})))

    def test_an_invalid_signature_is_nobody(self) -> None:
        """Fail-closed: un token che non verifica non promuove nessuno."""
        self.assertIsNone(agents._signed_actor(_Req({}, token="tok-falso")))


class TriggerProvenanceTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        ch._TRIGGERED.clear()
        self.addCleanup(ch._TRIGGERED.clear)
        from ..colony import pki
        p = patch.object(pki, "verify_session_token", side_effect=_verifica)
        p.start()
        self.addCleanup(p.stop)

    async def _trigger(self, by: str, token: str | None,
                       text: str = "che ne pensate?") -> tuple[dict, list]:
        meta = {"owner": "davide", "participants": ["clodia", "fullstack-dev"],
                "tier": "SEAL-1"}
        avviati: list = []

        def _fake_turn(tier, name, meta, **kw):
            avviati.append(kw)

            async def _noop():
                return ("clodia", "ok")
            return _noop()

        req = _Req({"text": text, "by": by}, token=token)
        with patch.object(ch.topics_client, "open_topic", return_value={"meta": meta}), \
             patch.object(ch.registry, "get_by_name",
                          side_effect=lambda n: (_Spec(_TIPI[n]) if n in _TIPI else None)), \
             patch.object(ch, "_spawn_bg", side_effect=lambda coro: coro.close()), \
             patch.object(ch, "run_topic_turn", new=_fake_turn):
            out = await ch.channel_trigger_internal("SEAL-1", "software-house", req)
        return out, avviati

    async def test_a_person_behind_a_carrier_token_is_human(self) -> None:
        """Il difetto della #413 nella sua forma più corta: prima era un 403."""
        out, avviati = await self._trigger("davide", token="tok-davide")
        self.assertTrue(out["triggered"])
        self.assertEqual(out["kind"], "human")
        self.assertEqual(len(avviati), 1)
        self.assertEqual(avviati[0]["trigger_kind"], "human")
        # Senza `human` il turno non sceglierebbe nessun responder per rilevanza
        # (`run_topic_turn`: `if _tag is None and _kind_eff != "human"`).
        self.assertEqual(avviati[0]["directive"], "",
                         "una persona della colonia non è provenienza non fidata")

    async def test_an_agent_token_is_ai(self) -> None:
        out, avviati = await self._trigger("clodia", token="tok-clodia")
        self.assertTrue(out["triggered"])
        self.assertEqual(out["kind"], "ai")
        self.assertEqual(avviati[0]["directive"], "")

    async def test_without_a_bearer_it_stays_external(self) -> None:
        """Il comportamento di oggi non cambia: senza firma, fail-closed."""
        out, avviati = await self._trigger("clodia", token=None)
        self.assertEqual(out["kind"], "external")
        self.assertTrue(avviati[0]["directive"],
                        "la direttiva di provenienza non fidata resta (#221)")

    async def test_the_impersonation_check_survives(self) -> None:
        """Un token NON on-behalf firmato `clodia` non può dichiararsi `davide`."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as e:
            await self._trigger("davide", token="tok-clodia")
        self.assertEqual(e.exception.status_code, 403)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
