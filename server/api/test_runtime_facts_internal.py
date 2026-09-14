"""Provider/modello/SEAL effettivi di un agente in uno scope, per un annuncio
DETERMINISTICO — non un turno, non un'impressione lasciata al modello (Davide,
14 set 2026: «deve essere deterministico, no random»).

`clodia-tools` (`TopicService.add_participant`) non ha questi dati: vivono
nella risoluzione runtime di `clodia-logic`, la stessa già usata per il chip
"provider · modello" della webui (clodia-platform#310/#315). Questa rotta li
presta via il canale server-to-server già in uso per `announce/internal`
(`_paired_gateway`), non introduce un secondo modo di autenticarsi.
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import channels as ch

SECRET = "CLODIA_ORCHESTRATOR_SECRET"


def _env(secret: str | None):
    env = {k: v for k, v in os.environ.items() if k != SECRET}
    if secret:
        env[SECRET] = secret
    return patch.dict(os.environ, env, clear=True)


def _request(body: dict, *, secret: str | None = None):
    async def _json():
        return body

    req = type("R", (), {})()
    req.json = _json
    req.headers = {"x-orchestrator-secret": secret} if secret else {}
    return req


def _spec(name: str, type_: str = "normal") -> SimpleNamespace:
    return SimpleNamespace(name=name, type=type_)


_UNSET = object()


class RuntimeFactsInternalTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: dict, *, spec=None, runtime: dict | None = None,
                    seal: str | None = "SEAL-2", topic=_UNSET, server_secret=None):
        with _env(server_secret), \
                patch.object(ch.registry, "get_by_name", return_value=spec), \
                patch.object(ch.topics_client, "async_open_topic",
                            return_value=(topic if topic is not _UNSET
                                         else {"meta": {"tier": body.get("tier")}})), \
                patch.object(ch, "_topic_runtime",
                            return_value=runtime if runtime is not None else {}), \
                patch("server.api.providers.provider_seal", return_value=seal):
            return await ch.channel_runtime_facts_internal(_request(body))

    async def test_a_bot_with_an_eligible_provider_gets_the_facts(self) -> None:
        out = await self._call(
            {"tier": "SEAL-1", "name": "tomato-blogging", "agent": "content-creator"},
            spec=_spec("content-creator"),
            runtime={"provider": "anthropic-api", "model": "claude-sonnet-4-5"},
            seal="SEAL-2",
        )
        self.assertEqual(out, {
            "is_bot": True, "eligible": True,
            "provider": "anthropic-api", "model": "claude-sonnet-4-5",
            "seal": "SEAL-2",
        })

    async def test_a_bot_with_no_eligible_provider_says_so_explicitly(self) -> None:
        """`_topic_runtime` vuoto = nessun provider connesso regge questo tier:
        il chiamante deve poterlo dire, non mostrare valori come se fossero
        validi o tacere."""
        out = await self._call(
            {"tier": "SEAL-3", "name": "riservato", "agent": "content-creator"},
            spec=_spec("content-creator"), runtime={},
        )
        self.assertEqual(out, {"is_bot": True, "eligible": False})

    async def test_a_human_gets_no_provider_facts(self) -> None:
        out = await self._call(
            {"tier": "SEAL-1", "name": "ops", "agent": "davide"},
            spec=_spec("davide", "human"),
        )
        self.assertEqual(out, {"is_bot": False})

    async def test_a_proxy_gets_no_provider_facts(self) -> None:
        out = await self._call(
            {"tier": "SEAL-1", "name": "ops", "agent": "github-hook"},
            spec=_spec("github-hook", "proxy"),
        )
        self.assertEqual(out, {"is_bot": False})

    async def test_missing_fields_are_rejected(self) -> None:
        with self.assertRaises(ch.HTTPException) as e:
            await self._call({"tier": "SEAL-1", "name": "ops"})
        self.assertEqual(e.exception.status_code, 400)

    async def test_an_unknown_agent_is_404(self) -> None:
        with self.assertRaises(ch.HTTPException) as e:
            await self._call(
                {"tier": "SEAL-1", "name": "ops", "agent": "fantasma"},
                spec=None,
            )
        self.assertEqual(e.exception.status_code, 404)

    async def test_an_unknown_topic_is_404(self) -> None:
        with self.assertRaises(ch.HTTPException) as e:
            await self._call(
                {"tier": "SEAL-1", "name": "fantasma", "agent": "content-creator"},
                spec=_spec("content-creator"), topic=None,
            )  # topic=None esplicito: async_open_topic torna None → 404
        self.assertEqual(e.exception.status_code, 404)

    async def test_the_shared_secret_is_enforced_when_configured(self) -> None:
        with self.assertRaises(ch.HTTPException) as e:
            await self._call(
                {"tier": "SEAL-1", "name": "ops", "agent": "content-creator"},
                spec=_spec("content-creator"), runtime={"provider": "x", "model": "y"},
                server_secret="s3cr3t",
            )
        self.assertEqual(e.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
