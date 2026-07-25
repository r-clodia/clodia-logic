from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from . import process_reaper


def _proc(root: Path, pid: int, ppid: int, started: int, cwd: Path, command: str):
    d = root / str(pid)
    d.mkdir()
    # stat_tail indices: ppid=1, starttime=19.
    tail = ["S", str(ppid)] + ["0"] * 17 + [str(started)]
    (d / "stat").write_text(f"{pid} (runtime worker) " + " ".join(tail))
    (d / "statm").write_text("100 10")
    (d / "cmdline").write_bytes(command.replace(" ", "\0").encode() + b"\0")
    (d / "cwd").symlink_to(cwd)


class ProcessReaperTests(unittest.TestCase):
    def test_only_stale_unmanaged_claude_descendants_are_terminated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uptime").write_text("10000 0")
            live = root / "live"
            orphan = root / "orphan"
            live.mkdir()
            orphan.mkdir()
            _proc(root, 2, 1, 100, live, "/usr/bin/claude chat")
            _proc(root, 3, 1, 100, orphan, "/opt/claude-code chat")
            _proc(root, 4, 99, 100, orphan, "/usr/bin/claude chat")
            _proc(root, 5, 1, 999999, orphan, "/usr/bin/claude chat")
            killed = []

            stats = process_reaper.sweep_orphan_runtime_processes(
                {str(live)}, 100, proc_root=root, root_pid=1,
                kill=lambda pid, sig: killed.append((pid, sig)),
            )

            self.assertEqual([pid for pid, _ in killed], [3])
            self.assertEqual(stats["live_processes"], 3)
            self.assertEqual(stats["reaped"], 1)

    def test_non_linux_proc_returns_no_processes(self):
        with TemporaryDirectory() as tmp:
            stats = process_reaper.sweep_orphan_runtime_processes(
                set(), 100, proc_root=Path(tmp), root_pid=1,
            )
            self.assertEqual(stats["live_processes"], 0)


if __name__ == "__main__":
    unittest.main()
