"""Riclassificazione di un topic, lato agent-server (clodia-platform#426).

Chi: una PERSONA owner del topic o admin, in entrambi i versi. Come: livello
valido, motivazione e presa di responsabilità esplicite. Dopo: i job del topic
seguono il livello e le sessioni sul vecchio livello si chiudono.
"""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from . import topics as T

META = {"tier": "SEAL-1", "owner": "giovanni",
        "participants": {"giovanni": "owner", "clodia": "contributor", "segretario": "contributor"}}


class _Req:
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        return self._b


def _spec(nome, seal):
    return types.SimpleNamespace(name=nome, type="bot", seal=seal)


class Base(unittest.TestCase):
    UMANI = {"giovanni", "davide"}
    ADMIN = {"davide"}

    def run_as(self, chi, coro_fn, **extra):
        from . import channels
        p = [patch.object(T, "_principal_from_request", lambda r: chi),
             patch.object(T.admin, "is_admin", lambda n: n in self.ADMIN),
             patch.object(channels, "_is_human_principal", lambda n: n in self.UMANI),
             patch.object(T.topics_client, "open_topic", lambda t, n: {"meta": dict(META)}),
             patch.object(channels, "_eligibility",
                          lambda spec, tier: {"eligible": int(tier[-1]) <= spec.seal}),
             patch("server.api.agent_registry.registry.get_by_name",
                   lambda n: {"clodia": _spec("clodia", 2), "segretario": _spec("segretario", 3)}.get(n))]
        for x in p:
            x.start()
        try:
            return asyncio.run(coro_fn())
        finally:
            for x in reversed(p):
                x.stop()


class AuthorityTests(Base):
    def _post(self, chi, body):
        gw = AsyncMock(return_value={"from": "SEAL-1", "to": body.get("tier"), "name": "acme"})
        with patch.object(T.topics_client, "async_set_topic_tier", gw), \
                patch("server.scheduler.db.retier_topic_jobs", return_value=[]), \
                patch("server.sdk_runtime.session.manager") as m:
            m.list.return_value = []
            res = self.run_as(chi, lambda: T.topic_set_tier("SEAL-1", "acme", _Req(body)))
        return res, gw

    BODY = {"tier": "SEAL-2", "reason": "preventivo del cliente", "accept_responsibility": True}

    def test_the_owner_reclassifies(self):
        res, gw = self._post("giovanni", dict(self.BODY))
        self.assertEqual(res["to"], "SEAL-2")
        gw.assert_awaited_once_with("SEAL-1", "acme", "SEAL-2", "giovanni", "preventivo del cliente")

    def test_an_admin_who_is_not_the_owner_reclassifies_too(self):
        res, _gw = self._post("davide", dict(self.BODY, tier="SEAL-0"))
        self.assertEqual(res["to"], "SEAL-0")

    def test_neither_owner_nor_admin_is_refused(self):
        self.UMANI = {"giovanni", "davide", "matteo"}
        with self.assertRaises(HTTPException) as e:
            self._post("matteo", dict(self.BODY))
        self.assertEqual(e.exception.status_code, 403)

    def test_an_agent_is_refused_even_if_it_owns_the_topic(self):
        with self.assertRaises(HTTPException) as e:
            self._post("clodia", dict(self.BODY))
        self.assertEqual(e.exception.status_code, 403)

    def test_reason_and_responsibility_are_required(self):
        for body in ({"tier": "SEAL-2", "accept_responsibility": True},
                     {"tier": "SEAL-2", "reason": "x"},
                     {"tier": "SEAL-2", "reason": "x", "accept_responsibility": "yes"},
                     {"tier": "SEAL-7", "reason": "x", "accept_responsibility": True}):
            with self.subTest(body=body):
                with self.assertRaises(HTTPException) as e:
                    self._post("giovanni", body)
                self.assertEqual(e.exception.status_code, 400)


class AfterTests(Base):
    def test_jobs_follow_and_old_sessions_close(self):
        gw = AsyncMock(return_value={"from": "SEAL-1", "to": "SEAL-3", "name": "acme"})
        chats = [types.SimpleNamespace(chat_id="chan:SEAL-1:acme:clodia"),
                 types.SimpleNamespace(chat_id="chan:SEAL-1:altro:clodia")]
        with patch.object(T.topics_client, "async_set_topic_tier", gw), \
                patch("server.scheduler.db.retier_topic_jobs", return_value=[7]) as rj, \
                patch("server.sdk_runtime.session.manager") as m:
            m.list.return_value = chats
            m.delete = AsyncMock()
            res = self.run_as("giovanni", lambda: T.topic_set_tier("SEAL-1", "acme", _Req(
                {"tier": "SEAL-3", "reason": "x", "accept_responsibility": True})))
        rj.assert_called_once_with("SEAL-1", "SEAL-3", "acme")
        m.delete.assert_awaited_once_with("chan:SEAL-1:acme:clodia")
        self.assertEqual(res["jobs"], [7])

    def test_the_preview_names_who_loses_access(self):
        res = self.run_as("giovanni", lambda: T.topic_tier_preview("SEAL-1", "acme", _Req(), to="SEAL-3"))
        self.assertEqual(res["direction"], "up")
        self.assertEqual([r["name"] for r in res["lose_access"]], ["clodia"])
        self.assertEqual(res["gain_access"], [])


class JobsDbTests(unittest.TestCase):
    def test_the_trigger_is_renamed_with_the_tier(self):
        import tempfile
        from pathlib import Path
        from ..scheduler import db
        d = Path(tempfile.mkdtemp())
        with patch.object(db, "JOBS_DIR", d):
            j = db.create_topic_trigger("SEAL-1", "acme", "fai il punto", interval_minutes=30)
            db.create_topic_trigger("SEAL-1", "altro", "x", interval_minutes=30)
            self.assertEqual(db.retier_topic_jobs("SEAL-1", "SEAL-2", "acme"), [j["id"]])
            g = db.get_job(j["id"])
            self.assertEqual((g["topic_tier"], g["name"]), ("SEAL-2", "topic-trigger:SEAL-2/acme"))
            self.assertIsNotNone(db.get_topic_trigger("SEAL-1", "altro"))


if __name__ == "__main__":
    unittest.main()
