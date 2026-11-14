"""`mailfrom:` is not a relay (clodia-platform#503).

The defect was **textual**: a mandate said that an authorised chat sender is an
ingress «exactly like a mail sender», and whoever read it concluded that
writing `mailfrom:<address>` among the ingress entries made that sender's mail
arrive. Since #531 the courier does deliver mail into rooms, and since #576 two
things are needed: the mailbox authorised for the instance (`inbox:`) and the
sender the room listens to (`mailfrom:`); global entries are not enough. The
explanation lives in base-pack's `mail` skill (formerly comms-pack's
`check-email`, #683), which is what an agent reads when mail comes up; the
check stays on the TEXT, because if the wrong sentence comes back nothing else
fails.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from ..config import workspace_path

SKILL = Path(workspace_path(
    "catalogs/packs/base-pack/plugins/base-pack/skills/mail/SKILL.md"))

#: The equivalence that produced the wrong promise.
EQUIVALENCES = ("esattamente come un mittente email", "exactly like a mail sender")


class MailfromIsNotARelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def section(self) -> str:
        parts = self.text.split("\n## How mail arrives", 1)
        self.assertEqual(2, len(parts), "section «How mail arrives» is gone")
        return parts[1].split("\n## ", 1)[0]

    def test_mail_is_not_equated_with_a_chat(self) -> None:
        for e in EQUIVALENCES:
            self.assertNotIn(e, self.text)

    def test_the_two_entries_are_told_apart(self) -> None:
        section = self.section()
        self.assertIn("`inbox:<mailbox>`", section)
        self.assertIn("`mailfrom:<sender>`", section)
        self.assertIn("**only** the senders it listens to", section)

    def test_without_senders_nothing_arrives(self) -> None:
        self.assertIn("**nothing arrives**", self.section())

    def test_global_entries_are_not_enough(self) -> None:
        self.assertIn("Global entries and the room's participants are not", self.section())

    def test_agents_have_no_mail_verbs(self) -> None:
        self.assertIn("You have no `email.*` verbs", self.text)
        self.assertIn("topic.mail_check", self.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
