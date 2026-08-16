import asyncio
import unittest
from unittest.mock import patch

from server.api import gateway_admin, provider_store, topics_client


class AsyncGatewayClientTests(unittest.TestCase):
    def test_topics_client_async_wrapper_offloads_sync_call(self):
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        with patch.object(topics_client.asyncio, "to_thread", side_effect=fake_to_thread):
            with patch.object(topics_client, "list_messages", return_value=[]) as list_messages:
                result = asyncio.run(topics_client.async_list_messages("SEAL-1", "software-house", limit=7))

        self.assertEqual(result, [])
        self.assertEqual(calls[0][0], list_messages)
        self.assertEqual(calls[0][1], ("SEAL-1", "software-house", 7))

    def test_provider_store_async_wrapper_offloads_sync_call(self):
        with patch.object(provider_store.asyncio, "to_thread") as to_thread:
            to_thread.side_effect = lambda func, *args, **kwargs: func(*args, **kwargs)
            with patch.object(provider_store, "list_ids", return_value={"codex"}) as list_ids:
                result = asyncio.run(provider_store.list_ids_async())

        self.assertEqual(result, {"codex"})
        to_thread.assert_called_once_with(list_ids)

    def test_gateway_admin_async_wrapper_offloads_sync_call(self):
        with patch.object(gateway_admin.asyncio, "to_thread") as to_thread:
            to_thread.side_effect = lambda func, *args, **kwargs: func(*args, **kwargs)
            with patch.object(gateway_admin, "agent_verbs", return_value={"verbs": []}) as agent_verbs:
                result = asyncio.run(gateway_admin.agent_verbs_async("clodia"))

        self.assertEqual(result, {"verbs": []})
        to_thread.assert_called_once_with(agent_verbs, "clodia")


if __name__ == "__main__":
    unittest.main()
