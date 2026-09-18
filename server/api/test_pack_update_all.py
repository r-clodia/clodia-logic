"""«ci vorrebbe un tasto update all che per ognuno dei pack lancia sia
l'update che il setup. Il setup potrebbe essere logico e non agentico»
(Davide, 18 set 2026).

`update_all_packs` orchestra `_perform_update` (già esistente, riusato da
`update_pack` per il singolo pack) + `pack_ops_logical.run_logical_setup`
(nuovo, deterministico) per ogni pack con upstream. Questi test isolano
l'orchestrazione: `_perform_update` e il setup logico sono mockati, non è
compito di questo file riverificarli (hanno le proprie suite).
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from . import gateway_pdp, packs


class _Req:
    pass


def _con_upstream(nomi: list[str]) -> list[dict]:
    return [{"name": n, "has_upstream": True} for n in nomi]


class UpdateAllTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        p = patch.object(gateway_pdp, "require_authz_async",
                         new=AsyncMock(return_value="davide"))
        p.start()
        self.addCleanup(p.stop)
        p2 = patch("server.api.pack_mcp_mount._plugin_names", return_value=["demo"])
        p2.start()
        self.addCleanup(p2.stop)
        p3 = patch("server.api.pack_ops.declarations", return_value={})
        p3.start()
        self.addCleanup(p3.stop)
        p4 = patch("server.sdk_runtime.session.manager.drop_all",
                   new=AsyncMock(return_value=[]))
        p4.start()
        self.addCleanup(p4.stop)

    async def test_only_packs_with_upstream_are_touched(self) -> None:
        with patch.object(packs, "_list_packs",
                          return_value=(_con_upstream(["a"])
                                       + [{"name": "b", "has_upstream": False}])), \
             patch.object(packs, "_perform_update",
                         new=AsyncMock(return_value={"updated": "a", "version": "1.0"})) as pu, \
             patch("server.api.pack_ops_logical.run_logical_setup_async",
                  new=AsyncMock(return_value={"done": [], "gaps": []})):
            out = await packs.update_all_packs(_Req())
        pu.assert_awaited_once_with("a", "davide")
        self.assertEqual([r["name"] for r in out["packs"]], ["a"])

    async def test_a_failing_pack_does_not_block_the_others(self) -> None:
        async def _pu(name, principal):
            if name == "rotto":
                raise RuntimeError("update fallito: 502")
            return {"updated": name, "version": "1.0"}
        with patch.object(packs, "_list_packs",
                          return_value=_con_upstream(["rotto", "sano"])), \
             patch.object(packs, "_perform_update", side_effect=_pu), \
             patch("server.api.pack_ops_logical.run_logical_setup_async",
                  new=AsyncMock(return_value={"done": [], "gaps": []})):
            out = await packs.update_all_packs(_Req())
        by_name = {r["name"]: r for r in out["packs"]}
        self.assertFalse(by_name["rotto"]["updated"])
        self.assertIn("502", by_name["rotto"]["error"])
        self.assertTrue(by_name["sano"]["updated"])

    async def test_no_gaps_marks_setup_done(self) -> None:
        with patch.object(packs, "_list_packs", return_value=_con_upstream(["a"])), \
             patch.object(packs, "_perform_update",
                         new=AsyncMock(return_value={"updated": "a", "version": "1.0"})), \
             patch("server.api.pack_ops_logical.run_logical_setup_async",
                  new=AsyncMock(return_value={"done": ["pip:mcp"], "gaps": []})), \
             patch.object(packs, "record_setup_done") as rsd:
            out = await packs.update_all_packs(_Req())
        rsd.assert_called_once_with("a", by="davide")
        self.assertTrue(out["packs"][0]["setup_done"])
        self.assertEqual(out["packs"][0]["setup_actions"], ["pip:mcp"])

    async def test_gaps_leave_setup_pending_and_do_not_mark_done(self) -> None:
        with patch.object(packs, "_list_packs", return_value=_con_upstream(["a"])), \
             patch.object(packs, "_perform_update",
                         new=AsyncMock(return_value={"updated": "a", "version": "1.0"})), \
             patch("server.api.pack_ops_logical.run_logical_setup_async",
                  new=AsyncMock(return_value={"done": [],
                                              "gaps": [{"kind": "pip", "package": "x",
                                                       "detail": "404"}]})), \
             patch.object(packs, "record_setup_done") as rsd:
            out = await packs.update_all_packs(_Req())
        rsd.assert_not_called()
        self.assertFalse(out["packs"][0]["setup_done"])
        self.assertEqual(len(out["packs"][0]["setup_gaps"]), 1)

    async def test_a_failed_update_never_calls_the_logical_setup(self) -> None:
        """Il setup di un pack che l'update non è nemmeno riuscito ad
        aggiornare non ha senso: non deve tentare di provisionarlo."""
        with patch.object(packs, "_list_packs", return_value=_con_upstream(["rotto"])), \
             patch.object(packs, "_perform_update",
                         side_effect=RuntimeError("update fallito")), \
             patch("server.api.pack_ops_logical.run_logical_setup_async",
                  new=AsyncMock()) as setup:
            await packs.update_all_packs(_Req())
        setup.assert_not_awaited()

    async def test_agents_restart_once_per_batch_not_once_per_pack(self) -> None:
        with patch.object(packs, "_list_packs", return_value=_con_upstream(["a", "b"])), \
             patch.object(packs, "_perform_update",
                         new=AsyncMock(return_value={"updated": "x", "version": "1.0"})), \
             patch("server.api.pack_ops_logical.run_logical_setup_async",
                  new=AsyncMock(return_value={"done": [], "gaps": []})), \
             patch("server.sdk_runtime.session.manager.drop_all",
                  new=AsyncMock(return_value=["c1", "c2"])) as drop_all:
            out = await packs.update_all_packs(_Req())
        drop_all.assert_awaited_once()
        self.assertEqual(out["agents_restarted"], 2)


if __name__ == "__main__":
    unittest.main()
