#!/usr/bin/env python3
"""Clodia agent-server CLI — thin client to the local server.

Comandi: server start/stop/status, chat (un singolo turn), history, follow eventi.
Tutta la logica multi-agent-type è stata rimossa in 1.0-rc: c'è solo Clodia.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from server import __version__  # noqa: E402
from server.config import HOST, PORT, LOGS_DIR  # noqa: E402

BASE_URL = f"http://{HOST}:{PORT}"
PID_FILE = LOGS_DIR / "server.pid"
LOG_FILE = LOGS_DIR / "server.log"


POLICY_TEXT = (TOOL_ROOT / "POLICY.md").read_text() if (TOOL_ROOT / "POLICY.md").is_file() else ""


def _is_server_running() -> bool:
    try:
        r = httpx.get(f"{BASE_URL}/health", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False


def cmd_server(args) -> int:
    sub = args.server_action
    if sub == "start":
        if _is_server_running():
            print(f"server already running at {BASE_URL}")
            return 0
        venv_python = TOOL_ROOT / ".venv" / "bin" / "python"
        python_exe = str(venv_python) if venv_python.is_file() else sys.executable
        log_fh = open(LOG_FILE, "ab")
        env = os.environ.copy()
        # No ANTHROPIC_API_KEY: il subprocess `claude` usa OAuth Max via keychain.
        proc = subprocess.Popen(
            [python_exe, "-m", "server.main"],
            cwd=str(TOOL_ROOT),
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=env,
        )
        PID_FILE.write_text(str(proc.pid))
        for _ in range(50):
            time.sleep(0.2)
            if _is_server_running():
                print(f"server started at {BASE_URL} (pid={proc.pid}, log={LOG_FILE})")
                return 0
        print(f"server did not become healthy in 10s, see {LOG_FILE}")
        return 1
    if sub == "stop":
        if not PID_FILE.is_file():
            print("no pid file, server not running?")
            return 1
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"sent SIGTERM to pid={pid}")
        except ProcessLookupError:
            print(f"pid {pid} not found")
        PID_FILE.unlink(missing_ok=True)
        return 0
    if sub == "status":
        if _is_server_running():
            r = httpx.get(f"{BASE_URL}/health").json()
            print(f"OK — {BASE_URL} version={r['version']} commit={r.get('commit','?')}")
            return 0
        print(f"DOWN — {BASE_URL} not responding")
        return 1
    return 2


def _require_server():
    if not _is_server_running():
        print(f"server not running at {BASE_URL}. Run `clodia server start`.", file=sys.stderr)
        sys.exit(1)


def cmd_chat(args) -> int:
    _require_server()
    cid = args.chat or "default"
    r = httpx.post(
        f"{BASE_URL}/clodia/chats/{cid}/messages",
        json={"content": args.message},
        timeout=600.0,
    )
    if r.status_code >= 400:
        print(f"error: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    print(r.json().get("response", ""))
    return 0


def cmd_status(args) -> int:
    _require_server()
    items = httpx.get(f"{BASE_URL}/clodia/chats", timeout=5.0).json()
    if not items:
        print("nessuna chat attiva")
        return 0
    width = max(len(c["chat_id"]) for c in items)
    for c in items:
        print(f"{c['chat_id']:{width}}  {c['status']:12}  {c['title']}")
    return 0


def cmd_history(args) -> int:
    _require_server()
    cid = args.chat or "default"
    r = httpx.get(f"{BASE_URL}/clodia/chats/{cid}/history", timeout=10.0).json()
    for msg in r:
        print(f"[{msg.get('timestamp', '')[:19]}] {msg.get('role'):10} {msg.get('content', '')[:300]}")
    return 0


def cmd_follow(args) -> int:
    _require_server()
    with httpx.stream("GET", f"{BASE_URL}/clodia/events", timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                    print(f"[{payload.get('timestamp', '')[:19]}] {payload.get('type'):15} {json.dumps(payload.get('payload', {}))[:200]}")
                except Exception:
                    print(line)
    return 0


def cmd_new(args) -> int:
    _require_server()
    r = httpx.post(f"{BASE_URL}/clodia/chats", timeout=30.0)
    if r.status_code >= 400:
        print(f"error: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    c = r.json()
    print(f"new chat: {c['chat_id']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clodia",
        description="Clodia agent-server CLI — singleton Clodia session su /Users/erreclaudea/erre-claudia.",
    )
    p.add_argument("--version", action="version", version=f"clodia agent-server {__version__}")
    p.add_argument("--policy", action="store_true", help="Print operational policy and exit")

    sub = p.add_subparsers(dest="cmd")

    p_srv = sub.add_parser("server", help="Start/stop/status of the local server")
    p_srv.add_argument("server_action", choices=["start", "stop", "status"])
    p_srv.set_defaults(func=cmd_server)

    p_chat = sub.add_parser("chat", help="Send a message to a Clodia chat and print the reply")
    p_chat.add_argument("message")
    p_chat.add_argument("--chat", default="default", help="chat_id (default: 'default')")
    p_chat.set_defaults(func=cmd_chat)

    p_stat = sub.add_parser("status", help="List Clodia chats with their status")
    p_stat.set_defaults(func=cmd_status)

    p_hist = sub.add_parser("history", help="Show conversation history of a chat")
    p_hist.add_argument("--chat", default="default", help="chat_id (default: 'default')")
    p_hist.set_defaults(func=cmd_history)

    p_new = sub.add_parser("new", help="Create a new chat with Clodia")
    p_new.set_defaults(func=cmd_new)

    p_follow = sub.add_parser("follow", help="Follow Clodia events SSE live")
    p_follow.set_defaults(func=cmd_follow)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.policy:
        print(POLICY_TEXT)
        return 0
    if not args.cmd:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
