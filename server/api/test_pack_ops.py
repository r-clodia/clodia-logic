import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ..instance_profile import InstanceProfile, PackOpsConfig
from . import pack_ops
from . import packs


class PackOpsTest(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_is_enabled_by_default(self):
        chat = AsyncMock()
        with (
            patch.object(
                pack_ops,
                "declarations",
                return_value={"demo": {"requires": {"pip": ["mcp"]}, "datastores": [], "rag_collections": []}},
            ),
            patch(
                "server.instance_profile.load",
                return_value=InstanceProfile(),
            ),
            patch("server.sdk_runtime.session.known_kind", return_value=True),
            patch("server.sdk_runtime.session.manager.get", side_effect=KeyError),
            patch("server.sdk_runtime.session.manager.create", new_callable=AsyncMock, return_value=chat) as create,
        ):
            result = await pack_ops.trigger_reconcile("boot")

        self.assertEqual(result, {"triggered": True, "agent": "sysadmin", "plugins": ["demo"]})
        create.assert_awaited_once()
        chat.send_user_message_async.assert_awaited_once()

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
