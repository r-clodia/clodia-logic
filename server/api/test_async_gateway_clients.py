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
        # Il wrapper inoltra la chiamata NELLA FORMA in cui è stata scritta: gli
        # argomenti non vengono riordinati né i default espansi in posizionali.
        # Espanderli faceva arrivare alla funzione sincrona una chiamata diversa
        # da quella del chiamante — e chi la sostituisce (un fake, un adattatore)
        # la vedeva con più argomenti di quanti ne fossero stati passati.
        self.assertEqual(calls[0][1], ("SEAL-1", "software-house"))
        self.assertEqual(calls[0][2], {"limit": 7})

    def test_topics_client_async_wrapper_non_espande_i_default(self):
        """Il difetto concreto: `post_message(t, n, a, testo, kind=...)` arrivava
        alla funzione sincrona come `(t, n, a, testo, kind, attachments)`."""
        visto = {}

        def post_message(*args, **kwargs):
            visto["args"], visto["kwargs"] = args, kwargs
            return {}

        with patch.object(topics_client, "post_message", post_message):
            asyncio.run(topics_client.async_post_message(
                "SEAL-1", "software-house", "davide", "ciao", kind="human"))

        self.assertEqual(visto["args"], ("SEAL-1", "software-house", "davide", "ciao"))
        self.assertEqual(visto["kwargs"], {"kind": "human"})

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
