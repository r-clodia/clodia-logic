"""#642 — a 1:1 Telegram chat with the bot is addressed to it without a mention.

In a group the bot must be mentioned (or replied to): someone else may be
meant. In a chat that holds only the bot and the user there is no ambiguity
(owner, 30 Oct 2026). Who may write is still the sender vetting's to decide.
"""
from __future__ import annotations

import unittest

from .telegram_relay_adapter import TelegramRelayAdapter


class PrivateChatTests(unittest.TestCase):
    def setUp(self):
        self.a = TelegramRelayAdapter()

    def test_a_private_chat_needs_no_mention(self):
        self.assertTrue(self.a.addresses_bot(
            {"chat_type": "private", "text": "ciao, mi prepari il report?"}, ["segretario"]))

    def test_a_group_still_needs_the_mention_or_a_reply(self):
        for kind in ("group", "supergroup", None):
            with self.subTest(kind=kind):
                self.assertFalse(self.a.addresses_bot(
                    {"chat_type": kind, "text": "ciao a tutti"}, ["segretario"]))
                self.assertTrue(self.a.addresses_bot(
                    {"chat_type": kind, "text": "@clodia mi aiuti?"}, ["segretario"]))
                self.assertTrue(self.a.addresses_bot(
                    {"chat_type": kind, "text": "sì", "reply_to_bot": True}, ["segretario"]))

    def test_a_channel_post_is_not_a_private_chat(self):
        self.assertFalse(self.a.addresses_bot({"chat_type": "channel", "text": "news"}, []))


if __name__ == "__main__":
    unittest.main()
