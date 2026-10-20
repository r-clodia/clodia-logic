"""#581 — a job's unattended session never gets the native `Agent` tool.

Run 115 of «Daily digest GRC» launched four background subagents and ended
its turn after 19 s: the run closes with the turn, so the results never came
back and `jobs.report_status` was never called."""
from __future__ import annotations

import inspect
import unittest

from . import session as S


class UnattendedTests(unittest.TestCase):
    def test_background_work_is_denied_to_unattended_sessions(self):
        for t in ("Agent", "Workflow", "Monitor", "ScheduleWakeup", "CronCreate",
                  "RemoteTrigger", "TaskCreate"):
            self.assertIn(t, S._UNATTENDED_DENIED)

    def test_every_denied_name_is_a_real_native_tool(self):
        from . import native_tools as nt
        self.assertEqual(set(S._UNATTENDED_DENIED) - set(nt.KNOWN_FALLBACK), set())

    def test_the_denial_joins_the_blocklist_before_the_root_branch(self):
        """Same place as the seed's own denials, so the root permission
        callback knows it too (it reads `disallowed`)."""
        src = inspect.getsource(S)
        i = src.index("for t in _UNATTENDED_DENIED if getattr(self, \"unattended\", False)")
        self.assertLess(src.index("disallowed = list(_resolve_disallowed_tools"), i)
        self.assertLess(i, src.index("opts_kwargs[\"can_use_tool\"]"))

    def test_a_job_session_is_unattended(self):
        src = inspect.getsource(S)
        self.assertIn('chat.unattended = bool(run_id and str(run_id).startswith("job:"))', src)


if __name__ == "__main__":
    unittest.main()
