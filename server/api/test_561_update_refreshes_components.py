"""clodia-platform#561 — a pack Update moves its gateway component's pin.

The gateway runs a `target: gateway` component at the commit it pinned, and
moves the pin only when a HUMAN admin asks (`POST /internal/components/<pack>/
refresh`). That route was unreachable for the human (the webui proxies
`/tools/*`, not `/internal/*`), so after an Update the new component code never
ran. The Update is now the human asking, with the on-behalf token every UI
action uses; the gateway still decides.

Measured here:

1. a human admin's Update sends the refresh, per pack, with an on-behalf
   token, no `allow_downgrade`, and reports the new pin;
2. an agent principal, or a human who is not admin, sends nothing and the
   result says why;
3. a pack without gateway components sends nothing;
4. a gateway refusal, an unreachable gateway or an unreadable answer does
   not fail the Update, and a 200 that kept the old pin is not a success;
5. a bundle refreshes each of its packs that declares components;
6. `_perform_update` actually calls it.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from . import gateway_pdp, packs


def _single(name: str = "google-pack") -> dict:
    return {"kind": "pack", "pack": name,
            "gateway_components": {"declared": ["google"],
                                   "refresh": f"POST /internal/components/{name}/refresh"}}


class _Gateway:
    """An httpx transport standing in for the gateway."""

    def __init__(self, status: int = 200, fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.reason = ""
        self.raw: bytes | None = None
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail:
            raise httpx.ConnectError("down", request=request)
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw)
        pack = request.url.path.split("/")[3]
        return httpx.Response(self.status, json={"components": [
            {"pack": pack, "id": "google", "status": "loaded", "reason": self.reason,
             "commit": "abc123", "verbs": ["x"]}]})


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.gw = _Gateway()
        real = httpx.AsyncClient
        transport = httpx.MockTransport(self.gw.handler)
        for p in (
            patch.object(httpx, "AsyncClient",
                         side_effect=lambda **kw: real(transport=transport, **kw)),
            patch.object(gateway_pdp, "_gw_base", return_value="http://gw:7849"),
            patch.object(gateway_pdp, "_token", side_effect=lambda p: f"tok-for-{p}"),
            patch.object(gateway_pdp, "is_agent", side_effect=lambda n: n == "clodia"),
            patch.object(gateway_pdp, "human_role",
                         side_effect=lambda n: "admin" if n == "davide" else "user"),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_a_human_admin_update_moves_the_pin(self) -> None:
        result = _single()
        await packs._refresh_gateway_components(result, "davide")
        (req,) = self.gw.calls
        self.assertEqual(str(req.url), "http://gw:7849/internal/components/google-pack/refresh")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.headers["authorization"], "Bearer tok-for-davide")
        self.assertEqual(json.loads(req.content), {})  # never allow_downgrade
        self.assertEqual(result["gateway_components"]["refreshed"], {
            "ok": True, "components": [
                {"id": "google", "status": "loaded", "reason": "", "commit": "abc123"}]})

    async def test_an_agent_or_a_plain_user_sends_nothing(self) -> None:
        for who in ("clodia", "mario"):
            result = _single()
            await packs._refresh_gateway_components(result, who)
            self.assertFalse(result["gateway_components"]["refreshed"]["ok"])
            self.assertIn("human admin", result["gateway_components"]["refreshed"]["reason"])
        self.assertEqual(self.gw.calls, [])

    async def test_a_pack_without_components_sends_nothing(self) -> None:
        for result in ({"kind": "pack", "pack": "it-pack"}, None, {}):
            await packs._refresh_gateway_components(result, "davide")
        self.assertEqual(self.gw.calls, [])

    async def test_a_refusal_does_not_fail_the_update(self) -> None:
        self.gw.status = 401
        result = _single()
        await packs._refresh_gateway_components(result, "davide")
        self.assertEqual(result["gateway_components"]["refreshed"],
                         {"ok": False, "reason": "gateway answered 401"})

    async def test_an_unreachable_gateway_does_not_fail_the_update(self) -> None:
        self.gw.fail = True
        result = _single()
        await packs._refresh_gateway_components(result, "davide")
        self.assertFalse(result["gateway_components"]["refreshed"]["ok"])
        self.assertIn("unreachable", result["gateway_components"]["refreshed"]["reason"])

    async def test_a_200_that_kept_the_old_pin_is_not_a_success(self) -> None:
        self.gw.reason = "fetch: upstream unreachable"
        result = _single()
        await packs._refresh_gateway_components(result, "davide")
        self.assertFalse(result["gateway_components"]["refreshed"]["ok"])
        self.assertEqual(result["gateway_components"]["refreshed"]["components"][0]["reason"],
                         "fetch: upstream unreachable")

    async def test_an_unreadable_answer_does_not_fail_the_update(self) -> None:
        for raw in (b"not json", b"[1, 2]"):
            self.gw.raw = raw
            result = _single()
            await packs._refresh_gateway_components(result, "davide")
            self.assertEqual(result["gateway_components"]["refreshed"],
                             {"ok": False, "reason": "unreadable gateway answer"})

    async def test_a_bundle_refreshes_each_declaring_pack(self) -> None:
        result = {"kind": "packs", "packs": [
            _single("google-pack"), {"kind": "pack", "pack": "it-pack"},
            _single("comms-pack")]}
        await packs._refresh_gateway_components(result, "davide")
        self.assertEqual([r.url.path for r in self.gw.calls], [
            "/internal/components/google-pack/refresh",
            "/internal/components/comms-pack/refresh"])
        self.assertTrue(result["packs"][0]["gateway_components"]["refreshed"]["ok"])
        self.assertNotIn("gateway_components", result["packs"][1])
        self.assertTrue(result["packs"][2]["gateway_components"]["refreshed"]["ok"])


class UpdateWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_perform_update_refreshes_with_the_caller(self) -> None:
        installed = _single()
        with patch.object(packs, "_pack_upstream",
                          return_value={"repo": "r-clodia/clodia-logic", "ref": "main",
                                        "path": "catalogs/packs/google-pack"}), \
             patch.object(packs, "_download_upstream_tarball", return_value=Path("/x")), \
             patch.object(packs.pack_import, "install_pack_from_root", return_value=installed), \
             patch.object(packs.pack_mcp_mount, "auto_mount_imported_mcp"), \
             patch.object(packs.plugins_api, "invalidate_plugins"), \
             patch.object(packs.registry, "load"), \
             patch.object(packs, "set_setup_pending"), \
             patch.object(packs.pack_import, "PACKS_META_DIR", Path("/nonexistent")), \
             patch.object(packs, "_refresh_gateway_components", new=AsyncMock()) as rf:
            out = await packs._perform_update("google-pack", "davide")
        rf.assert_awaited_once_with(installed, "davide")
        self.assertEqual(out["updated"], "google-pack")


if __name__ == "__main__":
    unittest.main()
