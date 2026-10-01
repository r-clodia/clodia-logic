"""Reasoning is attached to the BUBBLE, and only if there is a bubble.

clodia-platform#484. The store (`agents/reasoning_log.py`) and the accumulator
(`sdk_runtime`) are tested elsewhere: here we look at the joint between the
two, the only point that knows both the reasoned text and the message that
appeared in the channel.

Two owner decisions are written here as tests, because they are exactly the
kind of thing a careless refactoring flips:

  - **the key is the LAST bubble of the turn.** A turn may yield several
    messages (#243) but the reasoning is one: attaching it to all of them
    would show it repeated, attaching it to the first would put it before the
    steps it describes;
  - **no ghost bubbles.** A turn that produced no message and no failure
    notice leaves nothing: the limit is accepted, and inventing a bubble to
    attach the reasoning to would be worse than the defect being fixed.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from . import channels


class _Chat:
    """Fake session: delivers blocks and leaves a reasoning to take."""

    principal = ""

    def __init__(self, blocks, reasoning="this is how I reasoned", error=None):
        self.blocks = list(blocks)
        self.reasoning = reasoning
        self.error = error

    async def send_user_message(self, _prompt: str) -> str:
        cb = getattr(self, "on_visible_block", None)
        if cb is not None:
            for b in self.blocks:
                await cb(b)
        if self.error:
            raise self.error
        return "\n\n".join(self.blocks)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.messages: list[dict] = []
        self.recorded: list[dict] = []

        def post(_tier, _name, author, text, kind="human", **_kw):
            row = {"id": f"msg-{len(self.messages) + 1}", "author": author,
                   "text": text, "kind": kind, "ts": str(len(self.messages) + 1)}
            self.messages.append(row)
            return row

        def record(tier, name, **kw):
            self.recorded.append({"tier": tier, "name": name, **kw})

        async def noop_async(*_a, **_kw):
            return None

        self._patches = [
            patch.object(channels.topics_client, "post_message", post),
            patch.object(channels.topics_client, "list_messages",
                         lambda *_a, **_kw: list(self.messages)),
            patch.object(channels, "_maybe_delegate", noop_async),
            patch.object(channels, "_typing", noop_async),
            patch.object(channels, "_channel_message", noop_async),
            patch.object(channels, "_topic_title", lambda *_a, **_kw: None),
            patch.object(channels, "_spawn_bg", lambda _c: _c.close()),
            patch.object(channels.reasoning_log, "record", record),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    async def _run(self, chat, reasoning=None):
        """Run the turn with `take_reasoning` returning `reasoning`."""
        value = reasoning if reasoning is not None else (
            {"text": chat.reasoning, "truncated": False} if chat.reasoning else None)
        with patch.object(channels, "take_reasoning", return_value=value):
            return await channels._run_and_post_response(
                "SEAL-1", "ops", "clodia", chat, "prompt")


class TheKey(_Base):
    async def test_a_single_reply_carries_its_reasoning(self) -> None:
        await self._run(_Chat(["Here is the answer."]))
        self.assertEqual(1, len(self.recorded))
        entry = self.recorded[0]
        self.assertEqual("msg-1", entry["message_id"])
        self.assertEqual("this is how I reasoned", entry["text"])
        self.assertEqual(("SEAL-1", "ops"), (entry["tier"], entry["name"]))

    async def test_with_several_bubbles_it_goes_on_the_last(self) -> None:
        with patch.dict(os.environ, {"CLODIA_BUBBLE_PER_BLOCK": "1"}):
            await self._run(_Chat(["Looking now.", "Found it.", "Done."]))
        self.assertEqual(["msg-3"], [v["message_id"] for v in self.recorded])

    async def test_the_recorded_spawn_is_the_bubble_author(self) -> None:
        """It must match the author shown on the bubble, or in the UI the
        reasoning would appear to belong to another instance."""
        await self._run(_Chat(["Answer."]))
        self.assertEqual(self.messages[-1]["author"], self.recorded[0]["spawn"])


class NoGhostBubbles(_Base):
    async def test_a_turn_without_reasoning_writes_nothing(self) -> None:
        await self._run(_Chat(["Answer."], reasoning=None))
        self.assertEqual([], self.recorded)

    async def test_reasoning_without_any_bubble_is_lost(self) -> None:
        """THE STATED LIMIT. Posting to the channel fails: no bubble appeared,
        and none is invented to attach the reasoning to.

        It also pins the order of operations: storing happens AFTER the
        message exists, never before — an entry pointing to an id that was
        never born would stay in the store with no bubble to open it from.
        """
        def boom(*_a, **_kw):
            raise RuntimeError("gateway down")

        with patch.object(channels.topics_client, "post_message", boom):
            outcome = await self._run(_Chat(["Answer."]),
                                      reasoning={"text": "a lot", "truncated": False})
        self.assertIsNone(outcome)
        self.assertEqual([], self.recorded)

    async def test_a_write_error_does_not_break_the_turn(self) -> None:
        """Storing reasoning is an extra: it must never fail the turn that
        produced it."""
        with patch.object(channels.reasoning_log, "record",
                          side_effect=OSError("disk full")):
            outcome = await self._run(_Chat(["Answer."]))
        self.assertEqual("Answer.", outcome)


class ThePerimeter(_Base):
    async def test_reasoning_does_not_end_up_in_the_activity_log(self) -> None:
        """The activity log is indexed by AGENT and does not know the tier:
        writing reasoning there would mix channels of different clearance in
        the same file (this fixes the comment that promised the opposite)."""
        with patch.object(channels.activity_log, "append") as act:
            await self._run(_Chat(["Answer."]))
        for call in act.call_args_list:
            self.assertNotIn("this is how I reasoned", str(call))


if __name__ == "__main__":
    unittest.main()
