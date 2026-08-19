import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ..instance_profile import InstanceProfile, PackOpsConfig
from . import pack_mcp_mount
from . import pack_ops
from . import packs


_DECLS = {"demo": {"requires": {"pip": ["mcp"]}, "datastores": [], "rag_collections": []}}


class PackOpsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Attempt state (#116 point C) must be isolated per test.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        pp = patch.object(pack_ops, "data_path",
                          side_effect=lambda rel: Path(self._tmp.name) / rel)
        pp.start()
        self.addCleanup(pp.stop)

    async def test_boot_reports_without_delivering_a_turn(self):
        """#116 point A: boot reports, it does not act.

        A turn at boot attempted verbs that are gated by definition (`mcp.add`,
        `packs.install_*`) from a session with no channel, and each attempt
        surfaced as an out-of-context consent popup. What is missing is
        deterministic, so boot records it and stops.
        """
        with (
            patch.object(pack_ops, "declarations", return_value=_DECLS),
            patch("server.instance_profile.load", return_value=InstanceProfile()),
            patch("server.sdk_runtime.session.known_kind", return_value=True),
            patch("server.sdk_runtime.session.manager.create",
                  new_callable=AsyncMock) as create,
        ):
            result = await pack_ops.trigger_reconcile("boot")

        self.assertFalse(result["triggered"])
        self.assertEqual(result["reason"], "boot: report-only")
        self.assertEqual(result["plugins"], ["demo"])
        create.assert_not_awaited()   # no session, no gate

    async def test_explicit_trigger_still_delivers_a_turn(self):
        chat = AsyncMock()
        with (
            patch.object(pack_ops, "declarations", return_value=_DECLS),
            patch("server.instance_profile.load", return_value=InstanceProfile()),
            patch("server.sdk_runtime.session.known_kind", return_value=True),
            patch("server.sdk_runtime.session.manager.get", side_effect=KeyError),
            patch("server.sdk_runtime.session.manager.create",
                  new_callable=AsyncMock, return_value=chat) as create,
        ):
            result = await pack_ops.trigger_reconcile("post-import")

        self.assertTrue(result["triggered"])
        self.assertEqual(result["agent"], "sysadmin")
        create.assert_awaited_once()
        chat.send_user_message_async.assert_awaited_once()

    async def test_identical_declarations_are_not_requested_twice(self):
        """#116 point C: with no memory, a pending setup was re-requested on every
        startup — six restarts in a morning, six identical bursts of gates."""
        chat = AsyncMock()
        ctx = lambda: (  # noqa: E731
            patch.object(pack_ops, "declarations", return_value=_DECLS),
            patch("server.instance_profile.load", return_value=InstanceProfile()),
            patch("server.sdk_runtime.session.known_kind", return_value=True),
            patch("server.sdk_runtime.session.manager.get", side_effect=KeyError),
            patch("server.sdk_runtime.session.manager.create",
                  new_callable=AsyncMock, return_value=chat),
        )
        for patches in (ctx(),):
            for pt in patches:
                pt.start()
            first = await pack_ops.trigger_reconcile("post-import")
            second = await pack_ops.trigger_reconcile("post-import")
            for pt in patches:
                pt.stop()

        self.assertTrue(first["triggered"])
        self.assertFalse(second["triggered"])
        self.assertIn("already requested", second["reason"])
        # one delivery, not two
        self.assertEqual(chat.send_user_message_async.await_count, 1)

    async def test_reconcile_can_be_disabled_by_profile(self):
        profile = InstanceProfile()
        profile.pack_ops = PackOpsConfig(enabled=False)
        with (
            patch.object(
                pack_ops,
                "declarations",
                return_value={"demo": {"requires": {"pip": ["mcp"]}, "datastores": [], "rag_collections": []}},
            ),
            patch("server.instance_profile.load", return_value=profile),
            patch("server.sdk_runtime.session.manager.create", new_callable=AsyncMock) as create,
        ):
            result = await pack_ops.trigger_reconcile("boot")

        self.assertEqual(result, {"triggered": False, "reason": "pack_ops disabilitato dal profilo"})
        create.assert_not_called()

    def test_declarations_include_mcp_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bandi-pack").mkdir()
            (root / "bandi-pack" / "plugin.yaml").write_text(
                "name: bandi-pack\n"
                "mcp_servers:\n"
                "  sedia:\n"
                "    command: python3\n"
                "    args: ['/datadir/plugins/bandi-pack/mcp/sedia_mcp.py']\n",
                encoding="utf-8",
            )
            (root / "solo-skill").mkdir()
            (root / "solo-skill" / "plugin.yaml").write_text(
                "name: solo-skill\n", encoding="utf-8")
            with patch.object(pack_ops, "data_path", return_value=str(root)):
                decls = pack_ops.declarations()

        # un plugin con SOLI mcp_servers è materia di riconciliazione…
        self.assertEqual(list(decls), ["bandi-pack"])
        self.assertEqual(list(decls["bandi-pack"]["mcp_servers"]), ["sedia"])
        # …uno di sole skill no.
        self.assertNotIn("solo-skill", decls)

    def test_prompt_lists_declared_mcp_servers(self):
        decls = {
            "bandi-pack": {
                "requires": {}, "datastores": [], "rag_collections": [],
                "mcp_servers": {"sedia": {"command": "python3", "args": ["x.py"]}},
            }
        }
        prompt = pack_ops._reconcile_prompt("boot", decls)

        self.assertIn("mcp_servers [sedia]", prompt)
        self.assertIn("mcp.add", prompt)

    def test_prompt_flags_rag_feature_off_instead_of_asking_for_rag_tools(self):
        decls = {
            "bandi-pack": {
                "requires": {}, "datastores": [],
                "rag_collections": [{"name": "eu-normativa", "resources": []}],
                "mcp_servers": {},
            }
        }
        on = pack_ops._reconcile_prompt("boot", decls, rag_enabled=True)
        off = pack_ops._reconcile_prompt("boot", decls, rag_enabled=False)

        self.assertIn("rag.create_collection", on)
        self.assertNotIn("PROVISIONALA", off)
        self.assertIn("feature `rag` è DISATTIVATA", off)

    def test_prompt_tells_unattended_runs_not_to_retry_gates(self):
        decls = {"demo": {"requires": {"pip": ["mcp"]}, "datastores": [],
                          "rag_collections": [], "mcp_servers": {}}}
        prompt = pack_ops._reconcile_prompt("boot", decls)

        self.assertIn("Verbi gated", prompt)
        self.assertIn("delega permanente", prompt)

    def test_prompt_hands_over_the_drift_instead_of_having_it_recomputed(self):
        """Il confronto esiste come funzione: rifarlo a occhio è lavoro doppio.

        `drift()` sa già cosa è dichiarato e non montato; se il prompt chiede
        all'LLM di ricostruirlo da `mcp.list` il turno riparte da zero, e può
        anche sbagliare il confronto.
        """
        decls = {"bandi-pack": {"requires": {}, "datastores": [],
                                "rag_collections": [],
                                "mcp_servers": {"sedia": {"command": "python3"}}}}
        d = pack_ops.drift(decls, mounted=["github"])

        prompt = pack_ops._reconcile_prompt("post-import", decls, drift_report=d)

        self.assertIn("GIÀ FATTO", prompt)
        self.assertIn("sedia (plugin bandi-pack)", prompt)
        self.assertNotIn("confronta i server dichiarati con `mcp.list`", prompt)

    def test_prompt_forbids_unmounting_the_unmanaged_on_its_own(self):
        """Un backend non dichiarato può essere dell'admin: non è roba da smontare."""
        decls = {"bandi-pack": {"requires": {}, "datastores": [],
                                "rag_collections": [],
                                "mcp_servers": {"sedia": {"command": "python3"}}}}
        d = pack_ops.drift(decls, mounted=["sedia", "aggiunto-a-mano"])

        prompt = pack_ops._reconcile_prompt("post-import", decls, drift_report=d)

        self.assertIn("aggiunto-a-mano", prompt)
        self.assertIn("NON si smontano d'iniziativa", prompt)
        self.assertIn("la decisione è dell'owner", prompt)

    def test_prompt_falls_back_to_manual_comparison_when_drift_is_unavailable(self):
        """Gateway muto: meglio il confronto a mano che un elenco inventato."""
        decls = {"bandi-pack": {"requires": {}, "datastores": [],
                                "rag_collections": [],
                                "mcp_servers": {"sedia": {"command": "python3"}}}}
        d = pack_ops.drift(decls, mounted=None)

        prompt = pack_ops._reconcile_prompt("post-import", decls, drift_report=d)

        self.assertIn("confronta i server dichiarati con `mcp.list`", prompt)
        self.assertIn("il gateway non ha risposto", prompt)
        self.assertNotIn("GIÀ FATTO", prompt)

    async def test_trigger_computes_the_drift_and_puts_it_in_the_turn(self):
        chat = AsyncMock()
        decls = {"bandi-pack": {"requires": {}, "datastores": [],
                                "rag_collections": [],
                                "mcp_servers": {"sedia": {"command": "python3"}}}}
        with (
            patch.object(pack_ops, "declarations", return_value=decls),
            patch("server.instance_profile.load", return_value=InstanceProfile()),
            patch("server.sdk_runtime.session.known_kind", return_value=True),
            patch("server.sdk_runtime.session.manager.get", side_effect=KeyError),
            patch("server.sdk_runtime.session.manager.create",
                  new_callable=AsyncMock, return_value=chat),
            patch.object(pack_mcp_mount, "mounted_backends",
                         return_value=["residuo"]) as mounted,
        ):
            result = await pack_ops.trigger_reconcile("post-import")

        self.assertTrue(result["triggered"])
        mounted.assert_called_once_with("platform")
        sent = chat.send_user_message_async.await_args.args[0]
        self.assertIn("sedia (plugin bandi-pack)", sent)   # da montare
        self.assertIn("residuo", sent)                     # non gestito

    async def test_trigger_survives_a_gateway_that_does_not_answer(self):
        """Il drift è un extra: se la lettura salta, il turno parte comunque."""
        chat = AsyncMock()
        with (
            patch.object(pack_ops, "declarations", return_value=_DECLS),
            patch("server.instance_profile.load", return_value=InstanceProfile()),
            patch("server.sdk_runtime.session.known_kind", return_value=True),
            patch("server.sdk_runtime.session.manager.get", side_effect=KeyError),
            patch("server.sdk_runtime.session.manager.create",
                  new_callable=AsyncMock, return_value=chat),
            patch.object(pack_mcp_mount, "mounted_backends",
                         side_effect=RuntimeError("gateway giù")),
        ):
            result = await pack_ops.trigger_reconcile("post-import")

        self.assertTrue(result["triggered"])
        chat.send_user_message_async.assert_awaited_once()

    def test_mcp_only_declarations_count_as_pack_ops(self):
        result = {
            "plugins": [
                {
                    "plugin": "bandi-pack",
                    "mcp_servers": {"sedia": {"command": "python3"}},
                }
            ]
        }

        self.assertTrue(packs._has_pack_ops_declarations(result))

    def test_rag_only_declarations_count_as_pack_ops(self):
        result = {
            "plugins": [
                {
                    "plugin": "bandi-pack",
                    "rag_collections": [{"name": "eu-normativa", "resources": []}],
                }
            ]
        }

        self.assertTrue(packs._has_pack_ops_declarations(result))


if __name__ == "__main__":
    unittest.main()
