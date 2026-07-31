import unittest
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
