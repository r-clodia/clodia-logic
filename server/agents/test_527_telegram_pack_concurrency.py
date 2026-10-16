"""clodia-logic#527 review — the pack backend is NOT one process.

The gateway's proxy starts a stdio process per call, so the in-process lock of
`telegram_core` let two concurrent `lease_acquire` both win the "exclusive"
lease, and two writers raced on the same temp file. Here N real processes
race for the same lease: exactly one must win, and the state must stay valid.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CORE = (Path(__file__).resolve().parents[2] / "catalogs" / "packs" / "comms-pack"
        / "plugins" / "comms-pack" / "mcp")

_CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
import telegram_core as T
T.api_call = lambda *a, **k: []          # no network: getUpdates returns nothing
real_save = T._save_state
def slow_save(st):                       # widen the read-modify-write window
    time.sleep(0.05)
    real_save(st)
T._save_state = slow_save
try:
    r = T.lease_acquire({"chat_id": "1", "agent": sys.argv[2]})
    print(json.dumps({"ok": True, "holder": r["holder"]}))
except PermissionError as e:
    print(json.dumps({"ok": False, "why": "held"}))
except Exception as e:
    print(json.dumps({"ok": False, "why": type(e).__name__ + ": " + str(e)}))
"""


@unittest.skipUnless(hasattr(os, "fork"), "needs POSIX flock")
class LeaseIsExclusiveAcrossProcessesTests(unittest.TestCase):
    def test_only_one_of_many_concurrent_processes_gets_the_lease(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "telegram-state.json"
            state.write_text(json.dumps({"offset": 0, "bot_username": "b",
                                         "last_error": None, "chats": {
                "1": {"title": "g", "queue": [], "lease": None,
                      "last_preview": "", "type": "group"}}}))
            env = {**os.environ, "CLODIA_TELEGRAM_STATE": str(state),
                   "TELEGRAM_BOT_TOKEN": "x"}
            procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(CORE),
                                       f"agent-{i}"], env=env,
                                      stdout=subprocess.PIPE, text=True)
                     for i in range(8)]
            results = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
            winners = [r for r in results if r["ok"]]
            others = [r for r in results if not r["ok"]]
            self.assertEqual(len(winners), 1, results)
            self.assertTrue(all(r["why"] == "held" for r in others), results)
            final = json.loads(state.read_text())
            self.assertEqual(final["chats"]["1"]["lease"]["holder"],
                             winners[0]["holder"])
            self.assertEqual([p.name for p in Path(d).glob("*.tmp")], [])


if __name__ == "__main__":
    unittest.main()
