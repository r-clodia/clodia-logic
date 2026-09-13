"""L'idoneità è APPARTENENZA, non un filtro di visualizzazione — #190.

Prima di questo diff la stessa domanda («questo agente regge il tier?») veniva
posta a un predicato solo, `_provider_seal_ok`, che mescola due fatti di natura
diversa:

  - *durevole*: fra i provider DICHIARATI dal seed ce n'è uno con SEAL ≥ tier;
  - *transitorio*: quel provider adesso è collegato e non in pausa.

Con un predicato solo le due conseguenze sbagliate arrivano insieme. In aggiunta
non c'era nessun controllo, quindi un agente sotto-tier poteva SEDERSI in una
stanza confidenziale (e la webui lo nascondeva, che è la peggiore delle due:
sembrava non esserci). E qualunque riconciliazione costruita su quel bit avrebbe
svuotato ogni stanza al primo `pause_provider` — è il sintomo dell'11 agosto, la
stanza che «sembra vuota» perché il filtro di display nasconde tutti.

I test qui sotto tengono separati i due predicati: uno per l'appartenenza
(`_declared_seal_ok`, che ignora connessione e pausa) e uno per il turno
(`_provider_seal_ok`, invariato).
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from ..agents.models import AgentSpec
from . import channels as C


def _bot(name: str, providers: list[str], model: str = "claude-sonnet-4",
         agent_sdk: str = "claude") -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": "bot",
        "model": model, "system_prompt": "s.md", "agent_sdk": agent_sdk,
        "providers": providers,
    })


def _human(name: str, clearance: str) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name,
        "type": "human", "clearance": clearance,
    })


# `anthropic-api` è SEAL-1 nel catalogo, `aws-region-eu` SEAL-2: la coppia
# minima per distinguere «dichiara abbastanza» da «non ci arriva».
SOTTO = _bot("worker", ["anthropic-api"])
SOPRA = _bot("avvocato", ["aws-region-eu"])
OWNER = "davide"


def _registry(*specs: AgentSpec):
    per_nome = {s.name: s for s in specs}
    return patch.object(C.registry, "get_by_name", lambda n: per_nome.get(n))


class _Req:
    """Request finta: al codice sotto test servono solo `json()` e l'identità,
    che viene da `_principal_from_request` (patchato)."""

    def __init__(self, body: dict | None = None):
        self._body = body or {}

    async def json(self) -> dict:
        return self._body


def _meta(participants: list[str], tier: str = "SEAL-2") -> dict:
    return {"owner": OWNER, "participants": list(participants), "tier": tier,
            "title": "stanza"}


class DuePredicatiTests(unittest.TestCase):
    """Il cardine: due domande diverse, due funzioni diverse."""

    def test_declared_eligibility_ignores_connection_and_pause(self) -> None:
        # nessun provider collegato: `_provider_seal_ok` dice no (giustamente:
        # adesso non può prendere un turno), l'appartenenza dice sì.
        with patch.object(C, "_topic_provider", lambda spec, tier: None):
            self.assertFalse(C._provider_seal_ok(SOPRA, "SEAL-2"))
            self.assertTrue(C._declared_seal_ok(SOPRA, "SEAL-2"))

    def test_a_stack_that_does_not_reach_the_tier_is_never_a_member(self) -> None:
        self.assertFalse(C._declared_seal_ok(SOTTO, "SEAL-2"))
        self.assertTrue(C._declared_seal_ok(SOTTO, "SEAL-1"))


class AggiuntaTests(unittest.IsolatedAsyncioTestCase):
    """I due ingressi dell'aggiunta, una guardia sola."""

    async def test_owner_cannot_seat_an_agent_below_the_tier(self) -> None:
        set_p = AsyncMock()
        with _registry(SOTTO), \
                patch.object(C, "_principal_from_request", lambda r: OWNER), \
                patch.object(C.topics_client, "async_open_topic",
                             AsyncMock(return_value={"meta": _meta([OWNER])})), \
                patch.object(C.topics_client, "async_set_participant", set_p):
            with self.assertRaises(HTTPException) as cm:
                await C.channel_add_participant("SEAL-2", "stanza",
                                                _Req({"agent": "worker"}))
        self.assertEqual(cm.exception.status_code, 409)
        # la ragione è misurata e nominata, non un «non idoneo» generico
        self.assertIn("SEAL-2", cm.exception.detail)
        self.assertIn("SEAL-1", cm.exception.detail)
        set_p.assert_not_awaited()

    async def test_an_agent_cannot_seat_another_below_the_tier(self) -> None:
        """Stessa regola dall'ingresso interno (gateway). La docstring di questo
        endpoint dichiarava l'esatto contrario: «un agente sotto-tier può entrare
        ma non risponde» — è la decisione che #190 ribalta."""
        set_p = AsyncMock()
        with _registry(SOTTO, SOPRA), \
                patch.object(C.topics_client, "async_open_topic",
                             AsyncMock(return_value={"meta": _meta([OWNER, "avvocato"])})), \
                patch.object(C.topics_client, "async_set_participant", set_p):
            with self.assertRaises(HTTPException) as cm:
                await C.channel_set_participant_internal(
                    "SEAL-2", "stanza",
                    _Req({"agent": "worker", "by": "avvocato", "add": True}))
        self.assertEqual(cm.exception.status_code, 409)
        set_p.assert_not_awaited()

    async def test_removal_from_the_internal_door_is_not_gated(self) -> None:
        """La guardia è sull'AGGIUNTA: rimuovere un agente non idoneo dev'essere
        sempre possibile, altrimenti chi è già dentro non esce più."""
        set_p = AsyncMock(return_value={"participants": [OWNER], "added": False})
        with _registry(SOTTO, SOPRA), \
                patch.object(C.topics_client, "async_open_topic",
                             AsyncMock(return_value={"meta": _meta([OWNER, "avvocato", "worker"])})), \
                patch.object(C.topics_client, "async_set_participant", set_p):
            await C.channel_set_participant_internal(
                "SEAL-2", "stanza",
                _Req({"agent": "worker", "by": "avvocato", "add": False}))
        set_p.assert_awaited()

    async def test_a_human_below_clearance_is_not_seated_either(self) -> None:
        """L'asse esiste già (`_can_access`): invitare in una stanza SEAL-2 un
        umano con clearance SEAL-1 è lo stesso difetto."""
        set_p = AsyncMock()
        with _registry(_human("matteo", "SEAL-1")), \
                patch.object(C, "_principal_from_request", lambda r: OWNER), \
                patch.object(C.topics_client, "async_open_topic",
                             AsyncMock(return_value={"meta": _meta([OWNER])})), \
                patch.object(C.topics_client, "async_set_participant", set_p):
            with self.assertRaises(HTTPException) as cm:
                await C.channel_add_participant("SEAL-2", "stanza",
                                                _Req({"agent": "matteo"}))
        self.assertEqual(cm.exception.status_code, 409)
        set_p.assert_not_awaited()


class CreazioneTests(unittest.TestCase):
    def test_an_ineligible_contact_agent_is_not_seated_at_creation(self) -> None:
        """Terzo ingresso, il meno evidente: alla creazione il contact agent
        entrava fra i partecipanti senza che nessuno guardasse il tier."""
        with _registry(SOTTO):
            meta = C._channel_meta({"contact_agent": "worker"}, OWNER,
                                   "stanza", "SEAL-2")
        self.assertEqual(meta["participants"], [OWNER])

    def test_an_eligible_contact_agent_is_seated(self) -> None:
        with _registry(SOPRA):
            meta = C._channel_meta({"contact_agent": "avvocato"}, OWNER,
                                   "stanza", "SEAL-2")
        self.assertEqual(meta["participants"], [OWNER, "avvocato"])


class RiconciliazioneTests(unittest.TestCase):
    def _reconcile(self, meta: dict, *specs: AgentSpec):
        self.rimossi_dal_gateway: list[str] = []
        self.messaggi: list[str] = []

        def _set(tier, name, agent, add=True, role=None):
            self.rimossi_dal_gateway.append(agent)
            restanti = [p for p in meta["participants"] if p != agent]
            return {"participants": restanti, "added": False}

        def _post(tier, name, author, text, kind="human", attachments=None):
            self.messaggi.append(text)
            return {"id": "m1", "text": text}

        with _registry(*specs), \
                patch.object(C.topics_client, "set_participant", _set), \
                patch.object(C.topics_client, "post_message", _post):
            return C.reconcile_membership("SEAL-2", "stanza", meta)

    def test_every_provider_paused_removes_nobody(self) -> None:
        """Il test dell'11 agosto. Con il predicato transitorio la stanza si
        sarebbe svuotata: un provider in pausa è un fatto di stato, non una
        perdita di titolo a stare nella stanza."""
        with patch.object(C, "_topic_provider", lambda spec, tier: None):
            rimossi = self._reconcile(_meta([OWNER, "avvocato"]), SOPRA)
        self.assertEqual(rimossi, [])
        self.assertEqual(self.rimossi_dal_gateway, [])

    def test_a_durable_loss_removes_and_says_who_and_why(self) -> None:
        rimossi = self._reconcile(_meta([OWNER, "worker", "avvocato"]),
                                  SOTTO, SOPRA)
        self.assertEqual(rimossi, ["worker"])
        self.assertEqual(self.rimossi_dal_gateway, ["worker"])
        self.assertEqual(len(self.messaggi), 1)
        testo = self.messaggi[0]
        self.assertIn("worker", testo)
        self.assertIn("SEAL-2", testo)      # il tier della stanza
        self.assertIn("SEAL-1", testo)      # il massimo che l'agente dichiara

    def test_an_unidentifiable_stack_is_reported_not_evicted(self) -> None:
        """Un seed il cui modello non combacia con nessun pattern del catalogo
        non ha candidati noti: è un difetto di configurazione, non la prova che
        non regga il tier. L'espulsione è l'unica operazione distruttiva qui e
        chiede una prova positiva."""
        ignoto = _bot("misterioso", [], model="modello-che-non-esiste")
        self.assertIsNone(C._declared_seal(ignoto))
        rimossi = self._reconcile(_meta([OWNER, "misterioso"]), ignoto)
        self.assertEqual(rimossi, [])
        self.assertEqual(self.messaggi, [])

    def test_an_unidentifiable_stack_is_still_refused_at_the_explicit_door(self) -> None:
        """Severi all'invito, prudenti nello sfratto: lì c'è una persona che
        legge l'errore e può correggere il seed."""
        ignoto = _bot("misterioso", [], model="modello-che-non-esiste")
        self.assertFalse(C._member_eligible(ignoto, "SEAL-2"))

    def test_humans_and_the_owner_are_never_removed_automatically(self) -> None:
        """Recinto deciso con l'owner: la clearance di una persona la cambia una
        persona. Sfrattarla in automatico sarebbe autorità silenziosa su un
        utente, e l'owner non si tocca mai."""
        meta = _meta([OWNER, "matteo", "worker"])
        rimossi = self._reconcile(meta, _human("matteo", "SEAL-1"),
                                  _human(OWNER, "SEAL-1"), SOTTO)
        self.assertEqual(rimossi, ["worker"])


class EligibilityEndpointTests(unittest.TestCase):
    def test_a_paused_provider_is_unavailable_not_ineligible(self) -> None:
        """I due campi che la UI legge: `eligible` è l'appartenenza (durevole),
        `available` è «può rispondere adesso». Prima erano lo stesso bit, e
        perciò un provider in pausa faceva sparire il partecipante."""
        with patch.object(C, "_topic_provider", lambda spec, tier: None), \
                patch.object(C, "_topic_provider_model", lambda spec, tier: None):
            e = C._eligibility(SOPRA, "SEAL-2")
        self.assertTrue(e["eligible"])
        self.assertFalse(e["available"])

    def test_a_stack_below_the_tier_is_neither(self) -> None:
        with patch.object(C, "_topic_provider", lambda spec, tier: None), \
                patch.object(C, "_topic_provider_model", lambda spec, tier: None):
            e = C._eligibility(SOTTO, "SEAL-2")
        self.assertFalse(e["eligible"])
        self.assertFalse(e["available"])

    def test_humans_stay_eligible_and_available(self) -> None:
        e = C._eligibility(_human("matteo", "SEAL-2"), "SEAL-2")
        self.assertTrue(e["eligible"])
        self.assertTrue(e["available"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
