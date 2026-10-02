"""A turn's reasoning outlives the turn (clodia-platform#484).

`thinking_chunk` used to be ONLY an event on the bus: whoever was not connected
while the turn ran had nothing left to reopen. Here we look at the seam where a
piece of reasoning becomes both a live event and text to store —
`_publish_reasoning` — and at the hand-over of that text at the end of the turn.

The on-disk store and its cap are in `agents/test_reasoning_log.py`: here we
only check that the accumulator exists, is per turn, and empties.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from . import session as S


class _Fake:
    """The part of a session the seam touches: nothing else is needed."""

    def __init__(self) -> None:
        self.chat_id = "chan:SEAL-1:software-house:clodia"


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TheSeamPublishesAndAccumulates(unittest.TestCase):
    def setUp(self) -> None:
        self.published: list = []
        p = patch.object(S.bus, "publish", new=AsyncMock(
            side_effect=lambda ev: self.published.append(ev)))
        self.pub = p.start()
        self.addCleanup(p.stop)

    def test_the_chunk_goes_out_on_the_bus_as_before(self) -> None:
        s = _Fake()
        _run(S._publish_reasoning(s, 0, "reasoning now"))
        self.assertEqual(["thinking_chunk"], [e.type for e in self.published])
        self.assertEqual("reasoning now", self.published[0].payload["delta"])
        self.assertEqual(s.chat_id, self.published[0].payload["chat_id"])

    def test_the_same_chunk_is_available_after_the_turn(self) -> None:
        """THE REPORTED DEFECT: once streaming ended nothing was left."""
        s = _Fake()
        _run(S._publish_reasoning(s, 0, "first step"))
        _run(S._publish_reasoning(s, 1, "second step"))
        taken = S.take_reasoning(s)
        self.assertIn("first step", taken["text"])
        self.assertIn("second step", taken["text"])

    def test_the_stored_text_is_the_stitched_one(self) -> None:
        """Not two copies with two formats: the history is what was seen
        streaming, block separators included (see `_ThinkSeam`)."""
        s = _Fake()
        _run(S._publish_reasoning(s, 0, "end of block."))
        _run(S._publish_reasoning(s, 1, "new block."))
        live = "".join(e.payload["delta"] for e in self.published)
        self.assertEqual(live, S.take_reasoning(s)["text"])
        self.assertIn("\n\n", live)

    def test_an_empty_delta_publishes_nothing(self) -> None:
        s = _Fake()
        _run(S._publish_reasoning(s, 0, ""))
        self.assertEqual([], self.published)
        self.assertIsNone(S.take_reasoning(s))

    def test_taking_empties(self) -> None:
        """The next turn must not inherit this one's reasoning: it would be
        the right reasoning attached to the wrong bubble."""
        s = _Fake()
        _run(S._publish_reasoning(s, 0, "from the previous turn"))
        self.assertIsNotNone(S.take_reasoning(s))
        self.assertIsNone(S.take_reasoning(s))

    def test_the_cap_already_applies_in_memory(self) -> None:
        """The buffer does not grow unbounded while waiting to be capped on
        disk: a runaway turn would hold hundreds of MB in the process."""
        s = _Fake()
        for i in range(400):
            _run(S._publish_reasoning(s, i, "x" * 1000))
        taken = S.take_reasoning(s)
        self.assertTrue(taken["truncated"])
        self.assertLess(len(taken["text"]), 200_000)

    def test_without_reasoning_nothing_is_taken(self) -> None:
        self.assertIsNone(S.take_reasoning(_Fake()))


class TheEmissionPointsGoThroughIt(unittest.TestCase):
    """A seam that a runtime bypasses is reasoning lost halfway.

    There are four points (Claude SDK, codex, opencode `reasoning`, opencode
    `text` diverted) and they already forgot the freshly born `_ThinkSeam`
    once: the same mistake here would mean a turn whose reasoning happened and
    cannot be found.
    """

    def _source(self) -> str:
        from pathlib import Path
        return (Path(__file__).parent / "session.py").read_text(encoding="utf-8")

    def test_all_four_call_the_seam(self) -> None:
        src = self._source()
        self.assertEqual(4, src.count("await _publish_reasoning("),
                         "there are four reasoning emission points")

    def test_none_publishes_a_thinking_chunk_on_its_own(self) -> None:
        src = self._source()
        self.assertEqual(
            1, src.count('type="thinking_chunk"'),
            "thinking_chunk is published ONLY inside _publish_reasoning: a "
            "second point would be reasoning that is not stored")

    def test_every_runtime_opens_its_accumulator_at_turn_start(self) -> None:
        """Three session classes, three `send_user_message`: if one does not
        reset, its reasoning accumulates for the whole life of the session."""
        src = self._source()
        self.assertEqual(3, src.count("_start_reasoning(self)"),
                         "there are three session classes")


if __name__ == "__main__":
    unittest.main()
