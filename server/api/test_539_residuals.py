"""Residui dei connettori dei pack, lato agent-server (clodia-platform#539).

Tre punti, tre invarianti eseguibili:

§1 il runner DICHIARA di essere il runner (claim firmato `role: agent-server`)
   invece di farsi riconoscere da ciò che il suo token non porta;
§2 legare un canale o una cartella a un topic è un atto di una PERSONA, anche
   quando il topic ha un agente come owner di default;
§6 un trasporto che non risponde viene ritentato con backoff e logga una volta,
   non a ogni giro del ciclo di poll.
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from ..colony import pki
from . import channel_relay
from . import topics as T


# ── §1 claim di servizio ─────────────────────────────────────────────────────
class ServiceRoleClaimTests(unittest.TestCase):
    """Il claim esiste, viaggia fino al gateway ed è una lista chiusa."""

    def test_il_token_del_runner_porta_il_ruolo_firmato(self):
        visto = {}

        def finto_mint(agent, execution_id, ttl_seconds, principal, clearance,
                       on_behalf, human_role, chat, scoped_key, unattended,
                       scope_tier=None, origin=None, role=None):
            visto["role"] = role
            return "ckt1.finto"

        with patch.dict("os.environ", {"CLODIA_ORCHESTRATOR_SECRET": "s3cr3t"}), \
             patch.object(pki, "_mint_request", finto_mint), \
             patch.dict(pki._MINT_CACHE, {}, clear=True):
            tok = pki.mint_session_token("clodia", ttl_seconds=300,
                                         role=pki.ROLE_AGENT_SERVER)
        self.assertEqual(tok, "ckt1.finto")
        self.assertEqual(visto.get("role"), "agent-server")

    def test_due_token_dello_stesso_principal_non_si_scambiano_in_cache(self):
        """Il token di servizio e quello runtime di `clodia` differiscono SOLO
        per `role`: se la chiave di cache lo ignora, il secondo chiamante si
        prende il token dell'altro — cioè l'autorità dell'altro."""
        coniati = []

        def finto_mint(*a, role=None, **k):
            coniati.append(role)
            return f"ckt1.{role}"

        with patch.dict("os.environ", {"CLODIA_ORCHESTRATOR_SECRET": "s3cr3t"}), \
             patch.object(pki, "_mint_request", finto_mint), \
             patch.dict(pki._MINT_CACHE, {}, clear=True):
            a = pki.mint_session_token("clodia", ttl_seconds=300,
                                       role=pki.ROLE_AGENT_SERVER)
            b = pki.mint_session_token("clodia", ttl_seconds=300)
        self.assertNotEqual(a, b)
        self.assertEqual(coniati, ["agent-server", None])

    def test_un_ruolo_inventato_non_si_firma(self):
        with self.assertRaises(PermissionError):
            pki.mint_session_token("clodia", role="admin")

    def test_il_client_dei_topic_chiede_il_ruolo(self):
        from . import topics_client
        with patch.object(pki, "mint_session_token",
                          lambda *a, **k: f"ckt1.{k.get('role')}") as _m:
            h = topics_client._headers()
        self.assertEqual(h["Authorization"], "Bearer ckt1.agent-server")

    def test_il_client_di_pack_import_chiede_il_ruolo(self):
        from . import gateway_admin
        with patch.object(pki, "mint_session_token",
                          lambda *a, **k: f"ckt1.{k.get('role')}"):
            h = gateway_admin._headers()
        self.assertEqual(h["Authorization"], "Bearer ckt1.agent-server")


# ── §2 atti sui muri: owner E persona ────────────────────────────────────────
META = {"tier": "SEAL-1", "owner": "clodia", "participants": {"clodia": "owner"}}


class _Req:
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        return self._b


class WallActionsAreHumanTests(unittest.TestCase):
    """Il topic ha `owner = clodia` (il default di un topic senza owner umano):
    è esattamente lo scenario del confused deputy della issue."""

    UMANI = {"davide", "giovanni"}
    ADMIN = {"davide"}

    def run_as(self, chi, coro_fn, meta=None):
        from . import channels
        p = [patch.object(T, "_principal_from_request", lambda r: chi),
             patch.object(T.admin, "is_admin", lambda n: n in self.ADMIN),
             patch.object(channels, "_is_human_principal", lambda n: n in self.UMANI),
             patch.object(T.topics_client, "open_topic",
                          lambda t, n: {"meta": dict(meta or META)})]
        for x in p:
            x.start()
        try:
            return asyncio.run(coro_fn())
        finally:
            for x in reversed(p):
                x.stop()

    def _tg_connect(self, chi, meta=None):
        gw = AsyncMock(return_value={"ok": True})
        with patch.object(T.topics_client, "async_telegram_link_action", gw):
            out = self.run_as(chi, lambda: T.telegram_link_action(
                "SEAL-1", "acme", _Req({"action": "connect", "chat_id": "-100777"})),
                meta=meta)
        return out, gw

    def test_il_token_runtime_di_clodia_non_lega_piu_una_chat(self):
        with self.assertRaises(HTTPException) as ctx:
            self._tg_connect("clodia")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_l_owner_umano_lega_una_chat(self):
        meta = dict(META, owner="giovanni")
        out, gw = self._tg_connect("giovanni", meta=meta)
        self.assertEqual(out, {"ok": True})
        gw.assert_awaited_once()

    def test_l_admin_umano_lega_una_chat(self):
        out, gw = self._tg_connect("davide")
        self.assertEqual(out, {"ok": True})
        gw.assert_awaited_once()

    def test_lo_stato_del_collegamento_resta_leggibile_all_umano(self):
        gw = AsyncMock(return_value={"linked": False})
        with patch.object(T.topics_client, "async_telegram_link_status", gw):
            out = self.run_as("davide", lambda: T.telegram_link_status(
                "SEAL-1", "acme", _Req()))
        self.assertEqual(out, {"linked": False})

    def test_lo_stato_del_collegamento_non_e_leggibile_da_un_agente(self):
        with patch.object(T.topics_client, "async_telegram_link_status",
                          AsyncMock(return_value={"linked": False})):
            with self.assertRaises(HTTPException) as ctx:
                self.run_as("clodia", lambda: T.telegram_link_status(
                    "SEAL-1", "acme", _Req()))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_la_cartella_condivisa_la_aggancia_una_persona(self):
        gw = AsyncMock(return_value={"ok": True})
        with patch.object(T.topics_client, "async_local_folder_action", gw):
            with self.assertRaises(HTTPException) as ctx:
                self.run_as("clodia", lambda: T.local_folder(
                    "SEAL-1", "acme", _Req({"action": "add"})))
            self.assertEqual(ctx.exception.status_code, 403)
            out = self.run_as("davide", lambda: T.local_folder(
                "SEAL-1", "acme", _Req({"action": "add"})))
        self.assertEqual(out, {"ok": True})


# ── §6 backoff del relay ─────────────────────────────────────────────────────
class _AdapterRotto:
    connector = "telegram"

    def __init__(self):
        self.tentativi = 0

    async def poll(self, timeout):
        self.tentativi += 1
        raise RuntimeError("gateway irraggiungibile")


class _AdapterSano:
    connector = "telegram"

    def __init__(self):
        self.tentativi = 0

    async def poll(self, timeout):
        self.tentativi += 1
        return []


class RelayBackoffTests(unittest.TestCase):
    BIND = {"telegram": {"-100777": {"tier": "SEAL-1", "topic": "acme"}}}

    def setUp(self):
        channel_relay._BACKOFF.clear()
        self.addCleanup(channel_relay._BACKOFF.clear)

    def _cicli(self, adapter, quanti, dormite):
        async def finta_sleep(s):
            dormite.append(s)

        with patch.object(channel_relay.cb, "load", lambda: dict(self.BIND)), \
             patch.object(channel_relay.relay_adapters, "get", lambda c: adapter), \
             patch.object(channel_relay.asyncio, "sleep", finta_sleep):
            for _ in range(quanti):
                asyncio.run(channel_relay.run_poll_cycle(25))

    def test_il_trasporto_rotto_non_si_interroga_a_ogni_giro(self):
        """Il cuore di #539 §6: 20 cicli, UN solo tentativo finché l'attesa non
        scade. Prima erano 20 tentativi e 20 warning in pochi millisecondi."""
        a = _AdapterRotto()
        dormite = []
        self._cicli(a, 20, dormite)
        self.assertEqual(a.tentativi, 1)
        self.assertEqual(len(dormite), 20)   # il ciclo attende invece di girare a vuoto

    def test_un_warning_per_guasto_non_uno_per_tentativo(self):
        a = _AdapterRotto()
        with self.assertLogs("agent-server.channel_relay", level="WARNING") as log:
            self._cicli(a, 20, [])
        warn = [r for r in log.records if r.levelno >= logging.WARNING]
        self.assertEqual(len(warn), 1, [r.getMessage() for r in warn])
        self.assertIn("telegram", warn[0].getMessage())

    def test_l_attesa_cresce_e_non_supera_il_tetto(self):
        a = _AdapterRotto()
        attese = [channel_relay._backoff_note_failure("telegram", RuntimeError("x"))
                  for _ in range(10)]
        self.assertEqual(attese[:3], [2.0, 4.0, 8.0])
        self.assertEqual(attese[-1], channel_relay._BACKOFF_MAX)
        self.assertEqual(a.tentativi, 0)

    def test_il_rientro_azzera_il_backoff_e_lo_dice_una_volta(self):
        channel_relay._backoff_note_failure("telegram", RuntimeError("x"))
        a = _AdapterSano()
        with self.assertLogs("agent-server.channel_relay", level="INFO") as log:
            channel_relay._BACKOFF["telegram"]["not_before"] = 0.0
            self._cicli(a, 3, [])
        self.assertEqual(a.tentativi, 3)
        self.assertNotIn("telegram", channel_relay._BACKOFF)
        rientri = [r for r in log.records if "tornato a rispondere" in r.getMessage()]
        self.assertEqual(len(rientri), 1)

    def test_un_trasporto_sano_non_attende_mai(self):
        a = _AdapterSano()
        dormite = []
        self._cicli(a, 5, dormite)
        self.assertEqual(a.tentativi, 5)
        self.assertEqual(dormite, [])


if __name__ == "__main__":
    unittest.main()
