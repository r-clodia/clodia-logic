"""Spostare i MURI di un topic è un atto di una persona (#539 §2).

`_require_topic_owner` confronta il principal — che è il claim `agent` del token
firmato — con `meta.owner`. Un topic creato senza owner esplicito ha
`owner = contact_agent` o `clodia` (`topics/service.py`), e il token runtime di
clodia sta nell'ambiente di OGNI spawn: «sono l'owner» era soddisfatto da chi
doveva essere limitato. Il danno non è teorico — da lì si lega una chat Telegram
a un canale e si scrivono ingress ed egress, senza che nessuno veda una card.

Il discrimine non è un permesso in più ma una natura: `type: human` nel registry,
che un agente non ha per costruzione. Qui il registry è VERO (una directory
temporanea con due `agent.yaml`), non una funzione finta: se domani la forma del
registry cambia, questi test se ne accorgono.

L'altra faccia, quella che costa di più sbagliare: l'owner umano e l'admin
devono continuare a passare. Una guardia che chiude anche la porta di casa è un
guasto, non una correzione.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from . import admin, channels as C, topics as T

META = {"tier": "SEAL-1", "owner": "clodia",
        "participants": {"clodia": "owner", "davide": "contributor"}}
META_UMANO = {"tier": "SEAL-1", "owner": "davide",
              "participants": {"davide": "owner"}}


class _Req:
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


class _Client:
    """Il gateway visto dall'agent-server: registra TUTTO ciò che riceve.

    Serve a provare la cosa che conta davvero — non il codice di risposta, ma
    che la richiesta rifiutata non sia comunque arrivata ai muri.
    """

    class TopicsClientError(RuntimeError):
        pass

    def __init__(self, meta=META):
        self.chiamate: list = []
        self.meta = meta

    def open_topic(self, tier, name):
        return {"meta": self.meta}

    def connector_action_status(self, tier, name, connector, action):
        self.chiamate.append(("tg-status", tier, name))
        return {"connected": False, "admin_only": False}

    def connector_action_apply(self, tier, name, connector, action, body):
        self.chiamate.append(("tg-action", tier, name, body.get("action")))
        return {"connected": True}

    def local_folder_action(self, tier, name, action, **params):
        self.chiamate.append(("local-folder", tier, name, action))
        return {"ok": True}

    def drive_folder_action(self, tier, name, action, **params):
        self.chiamate.append(("drive-folder", tier, name, action))
        return {"ok": True}

    def __getattr__(self, nome):
        if not nome.startswith("async_"):
            raise AttributeError(nome)
        sync = getattr(self, nome[len("async_"):])

        async def chiamata(*a, **k):
            return sync(*a, **k)

        return chiamata


class _Base(unittest.TestCase):
    """Registry vero: `davide` persona, `clodia` agente, `root` persona admin."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        reg = Path(self.tmp.name)
        for nome, corpo in (("davide", "type: human\nrole: user\n"),
                            ("root", "type: human\nrole: superadmin\n"),
                            ("clodia", "type: agent\n")):
            (reg / nome).mkdir()
            (reg / nome / "agent.yaml").write_text(corpo, encoding="utf-8")
        p = patch.object(admin, "AGENTS_DIR", reg)
        p.start()
        self.addCleanup(p.stop)
        self.cli = _Client()

    def esegui(self, coro, chi: str, modulo=T):
        async def _authz(request, tool):      # il PDP è un'altra guardia
            return None

        p = [patch.object(modulo, "topics_client", self.cli),
             patch.object(modulo, "_principal_from_request", lambda r: chi),
             patch.object(C, "require_authz_async", _authz)]
        for x in p:
            x.start()
        try:
            return asyncio.run(coro)
        finally:
            for x in reversed(p):
                x.stop()

    def rifiutata(self, coro, chi: str, modulo=T):
        with self.assertRaises(HTTPException) as e:
            self.esegui(coro, chi, modulo)
        self.assertEqual(403, e.exception.status_code)
        self.assertEqual([], self.cli.chiamate,
                         "rifiutata ma inoltrata lo stesso al gateway")


class AnAgentOwnerIsNotEnoughTests(_Base):
    def test_the_telegram_button_refuses_the_agent_that_owns_the_topic(self) -> None:
        self.rifiutata(T.connector_action_apply(
            "SEAL-1", "ops", "telegram", "telegram-link",
            _Req({"action": "connect", "chat_id": "-100777"})),
            "clodia")

    def test_even_reading_the_link_state_refuses_it(self) -> None:
        """Lo stato nomina la chat legata: è l'altra metà della stessa porta."""
        self.rifiutata(T.connector_action_status("SEAL-1", "ops", "telegram", "telegram-link", _Req()), "clodia")

    def test_local_folder_refuses_it_too(self) -> None:
        """Stessa classe: agganciare una cartella è dichiarare cosa il topic
        può leggere e scrivere oltre ai propri file."""
        self.rifiutata(T.local_folder(
            "SEAL-1", "ops", _Req({"action": "add", "mount": "x"})), "clodia")

    def test_drive_folder_refuses_it_too(self) -> None:
        self.rifiutata(C.channel_drive_folder(
            "SEAL-1", "ops", _Req({"action": "add", "folder": "abc"})),
            "clodia", modulo=C)

    def test_an_unknown_principal_is_not_a_person(self) -> None:
        """Fail-closed: chi non è nel registry non è nessuno."""
        self.cli.meta = {"tier": "SEAL-1", "owner": "fantasma",
                         "participants": {"fantasma": "owner"}}
        self.rifiutata(T.connector_action_apply(
            "SEAL-1", "ops", "telegram", "telegram-link",
            _Req({"action": "connect", "chat_id": "-1"})),
            "fantasma")


class ThePeopleStillGetThroughTests(_Base):
    """La metà che non deve rompersi: la pagina Topics della webui."""

    def setUp(self) -> None:
        super().setUp()
        self.cli.meta = META_UMANO

    def test_the_human_owner_connects_as_before(self) -> None:
        self.esegui(T.connector_action_apply(
            "SEAL-1", "ops", "telegram", "telegram-link",
            _Req({"action": "connect", "chat_id": "-100777"})),
            "davide")
        # The generic route reads the action's state first (`admin_only`).
        self.assertEqual([("tg-status", "SEAL-1", "ops"),
                          ("tg-action", "SEAL-1", "ops", "connect")], self.cli.chiamate)

    def test_the_human_owner_reads_the_state_as_before(self) -> None:
        self.esegui(T.connector_action_status("SEAL-1", "ops", "telegram", "telegram-link", _Req()), "davide")
        self.assertEqual([("tg-status", "SEAL-1", "ops")], self.cli.chiamate)

    def test_the_human_owner_mounts_a_local_folder_as_before(self) -> None:
        self.esegui(T.local_folder(
            "SEAL-1", "ops", _Req({"action": "add"})), "davide")
        self.assertEqual([("local-folder", "SEAL-1", "ops", "add")], self.cli.chiamate)

    def test_the_human_owner_declares_a_drive_folder_as_before(self) -> None:
        self.esegui(C.channel_drive_folder(
            "SEAL-1", "ops", _Req({"action": "add", "folder": "abc"})),
            "davide", modulo=C)
        self.assertEqual([("drive-folder", "SEAL-1", "ops", "add")], self.cli.chiamate)

    def test_a_platform_admin_who_is_not_the_owner_still_passes(self) -> None:
        """Serve per i topic il cui owner è un agente: senza questa strada
        nessuno potrebbe più collegarli."""
        self.esegui(T.connector_action_apply(
            "SEAL-1", "ops", "telegram", "telegram-link",
            _Req({"action": "connect", "chat_id": "-1"})), "root")
        # The generic route reads the action's state first (`admin_only`).
        self.assertEqual([("tg-status", "SEAL-1", "ops"),
                          ("tg-action", "SEAL-1", "ops", "connect")], self.cli.chiamate)


class TheRegistryTellsPeopleFromAgentsTests(_Base):
    def test_is_human_reads_the_type_not_the_role(self) -> None:
        self.assertTrue(admin.is_human("davide"))
        self.assertTrue(admin.is_human("root"))
        self.assertFalse(admin.is_human("clodia"))
        self.assertFalse(admin.is_human("nessuno"))
        self.assertFalse(admin.is_human(None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
