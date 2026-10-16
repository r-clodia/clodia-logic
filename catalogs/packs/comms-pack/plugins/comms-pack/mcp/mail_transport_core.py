#!/usr/bin/env python3
"""mail_transport — the IMAP/SMTP transport of comms-pack's `mail` connector.

clodia-platform#523. Unlike Telegram (#522), mail does NOT move its verbs into
the pack: the gateway keeps `email.*` and every decision that belongs to the
reference monitor — which mailbox credential a call uses, the mailbox
whitelist (`inbox:`/`outbox:`), strict ingress with its fail-closed audit
(#503), topic attachments in and out with their taint, the reply recipient.
Moving those into a pack process would weaken exactly what #503 pins.

What the pack provides is the TRANSPORT, and nothing else. With the instance
switch `connectors: {mail: pack}` the gateway calls the one tool here, `run`:

    run({argv: [str], files: {relpath: base64}, timeout: int})
      -> {"returncode": int, "stdout": str, "stderr": str}

1. a private temp dir (0700) is created for this call only;
2. each file is written at its RELATIVE path inside it (0600) — the gateway
   puts that call's mailbox credential under `secrets/` and any outgoing
   attachments wherever its argv points; absolute paths, `..` and anything
   that would resolve through a symlink are refused;
3. the bundled `email_client.py` (a copy of the gateway's own client) runs
   with argv, every literal `{dir}` in it replaced by the temp dir, a minimal
   environment (PATH, LANG, HOME=<tmp>) and `CLODIA_SECRETS_DIR=<tmp>/secrets`;
4. the temp dir is ALWAYS removed — on success, on failure, on timeout.

The process therefore never holds more than the credential of the call in
progress, never reads the gateway's vault and keeps no state between calls.
The raw output goes back to the gateway, which filters it. Nothing here logs
argv, credentials or file contents; stderr is trimmed and, best effort,
scrubbed of the secret values it was handed before it is returned.

The tool is gateway-internal: the gateway hides it from agents, who keep
calling `email.*`.

**Stdlib only**, like `telegram_core.py`: the MCP envelope is
`mail_transport_mcp.py`, so this can be tested without the gateway's `mcp`.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
CLIENT = HERE / "email_client.py"

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
#: Upper bounds on what one call may write: a credential and a few attachments,
#: not a filesystem.
MAX_FILES = 64
MAX_FILES_BYTES = 64 * 1024 * 1024
#: stderr is diagnostics, not data: keep the tail (where a traceback ends).
STDERR_LIMIT = 4096
#: Exit status reported when the client is killed for running past `timeout`
#: (the convention of coreutils' `timeout`).
TIMEOUT_RETURNCODE = 124

DIR_PLACEHOLDER = "{dir}"
REDACTED = "[redacted]"
#: JSON keys whose string values count as secrets for stderr scrubbing.
_SECRET_KEY_HINTS = ("pass", "secret", "token", "key", "credential", "auth")
_MIN_SECRET_LEN = 6


class TransportError(ValueError):
    """A request the transport refuses before running anything."""


# ── request validation ────────────────────────────────────────────────────

def _check_relpath(rel: object) -> PurePosixPath:
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise TransportError(f"invalid file path: {rel!r}")
    if "\\" in rel:
        raise TransportError(f"backslash in file path: {rel!r}")
    path = PurePosixPath(rel)
    if path.is_absolute() or (len(rel) > 1 and rel[1] == ":"):
        raise TransportError(f"absolute file path refused: {rel!r}")
    # Check the RAW components: PurePosixPath would quietly fold `./x` and
    # `a//b`, and a path the gateway did not write literally is not accepted.
    if any(p in ("..", ".", "") for p in rel.split("/")):
        raise TransportError(f"file path must be plain and relative: {rel!r}")
    return path


def _check_request(args: dict) -> tuple[list[str], dict[PurePosixPath, bytes], int]:
    argv = args.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise TransportError("argv must be a list of strings")
    if any("\x00" in a for a in argv):
        raise TransportError("NUL byte in argv")

    raw_files = args.get("files")
    if raw_files is None:
        raw_files = {}
    if not isinstance(raw_files, dict):
        raise TransportError("files must be an object {relpath: base64}")
    if len(raw_files) > MAX_FILES:
        raise TransportError(f"too many files (max {MAX_FILES})")
    files: dict[PurePosixPath, bytes] = {}
    total = 0
    for rel, b64 in raw_files.items():
        path = _check_relpath(rel)
        if not isinstance(b64, str):
            raise TransportError(f"file content must be base64 text: {rel!r}")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise TransportError(f"file content is not valid base64: {rel!r}") from None
        total += len(data)
        if total > MAX_FILES_BYTES:
            raise TransportError(f"files exceed {MAX_FILES_BYTES} bytes")
        files[path] = data

    timeout = args.get("timeout", DEFAULT_TIMEOUT)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TransportError("timeout must be an integer number of seconds")
    timeout = int(timeout)
    if timeout <= 0:
        raise TransportError("timeout must be positive")
    return argv, files, min(timeout, MAX_TIMEOUT)


# ── the private directory ─────────────────────────────────────────────────

def _write_files(root: Path, files: dict[PurePosixPath, bytes]) -> None:
    """Write each file under `root`, never following a link, never outside."""
    root_real = root.resolve()
    for rel, data in files.items():
        parent = root
        for part in rel.parts[:-1]:
            parent = parent / part
            try:
                os.mkdir(parent, 0o700)
            except FileExistsError:
                st = os.lstat(parent)
                if not stat.S_ISDIR(st.st_mode):
                    raise TransportError(f"path conflict at {rel}") from None
        target = parent / rel.parts[-1]
        # Belt and braces: everything under root was created here, so this
        # can only fail if something above went wrong.
        if root_real not in target.parent.resolve().parents \
                and target.parent.resolve() != root_real:
            raise TransportError(f"file path escapes the call directory: {rel}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:
            raise TransportError(f"duplicate or conflicting file path: {rel}") from None
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)


def _remove(root: Path) -> None:
    def _onerror(func, path, _exc):  # a 0500 dir left by the client, say
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass
    shutil.rmtree(root, onerror=_onerror)


# ── output hygiene ────────────────────────────────────────────────────────

def _secret_values(files: dict[PurePosixPath, bytes]) -> set[str]:
    """Best-effort list of secret strings handed to this call.

    Only files under `secrets/` count (attachments are data, not credentials):
    from JSON, every string value under a key that looks like a secret; from
    anything else, each non-trivial line.
    """
    out: set[str] = set()

    def walk(node, secretish=False):
        if isinstance(node, dict):
            for k, v in node.items():
                hint = any(h in str(k).lower() for h in _SECRET_KEY_HINTS)
                walk(v, secretish or hint)
        elif isinstance(node, list):
            for v in node:
                walk(v, secretish)
        elif isinstance(node, str) and secretish and len(node) >= _MIN_SECRET_LEN:
            out.add(node)

    for rel, data in files.items():
        if rel.parts[0] != "secrets":
            continue
        text = data.decode("utf-8", errors="replace")
        try:
            walk(json.loads(text))
        except ValueError:
            out.update(line.strip() for line in text.splitlines()
                       if len(line.strip()) >= _MIN_SECRET_LEN)
    return out


def _scrub(text: str, secrets: set[str]) -> str:
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def _trim(text: str, limit: int = STDERR_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "[…trimmed…]\n" + text[-limit:]


# ── the tool ──────────────────────────────────────────────────────────────

def _with_dir(argv: list[str], root: Path) -> list[str]:
    """`{dir}` resolved ONLY where the gateway put it: the value of an
    `--attachment` flag that starts with `{dir}/`.

    Subject, body and recipients are text an agent wrote; replacing `{dir}`
    there silently altered the outgoing message and leaked this directory to
    an outside recipient (review of clodia-logic#528).
    """
    out: list[str] = []
    prefix = DIR_PLACEHOLDER + "/"
    for i, a in enumerate(argv):
        if i > 0 and argv[i - 1] == "--attachment" and a.startswith(prefix):
            out.append(str(root) + a[len(DIR_PLACEHOLDER):])
        else:
            out.append(a)
    return out


def run(args: dict) -> dict:
    """Run the bundled email client once, in a throwaway directory."""
    argv, files, timeout = _check_request(args or {})
    # Resolved, so `{dir}`, cwd and CLODIA_SECRETS_DIR name the same path
    # (on macOS the temp root sits behind the /tmp → /private/tmp link).
    root = Path(tempfile.mkdtemp(prefix="mail-transport-")).resolve()
    try:
        os.chmod(root, 0o700)
        _write_files(root, files)
        secrets_dir = root / "secrets"
        env = {
            "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
            "LANG": os.environ.get("LANG") or "C.UTF-8",
            "HOME": str(root),
            "CLODIA_SECRETS_DIR": str(secrets_dir),
        }
        cmd = [sys.executable, str(CLIENT), *_with_dir(argv, root)]
        proc = subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()
            returncode = TIMEOUT_RETURNCODE
            err = (err or b"") + f"\nmail-transport: timed out after {timeout}s".encode()
        secrets = _secret_values(files)
        stderr = _trim(_scrub(err.decode("utf-8", errors="replace"), secrets))
        return {
            "returncode": returncode,
            "stdout": out.decode("utf-8", errors="replace"),
            "stderr": stderr,
        }
    finally:
        _remove(root)


TOOL_SPECS: list[dict] = [
    dict(name="run",
         description=(
             "GATEWAY-INTERNAL — hidden from agents, who use email.*. Runs the "
             "bundled IMAP/SMTP client once: writes `files` (relative path → "
             "base64; the call's mailbox credential goes under secrets/) into a "
             "private temp dir, replaces '{dir}' in argv with that dir, runs "
             "email_client.py with CLODIA_SECRETS_DIR=<dir>/secrets, removes "
             "the dir. Returns JSON {returncode, stdout, stderr}."),
         inputSchema={
             "type": "object",
             "properties": {
                 "argv": {"type": "array", "items": {"type": "string"},
                          "description": "Arguments for email_client.py; "
                                         "'{dir}' is replaced by the call dir."},
                 "files": {"type": "object",
                           "additionalProperties": {"type": "string"},
                           "description": "Relative path → base64 content."},
                 "timeout": {"type": "integer", "minimum": 1,
                             "maximum": MAX_TIMEOUT, "default": DEFAULT_TIMEOUT,
                             "description": "Seconds (default 60, max 300)."},
             },
             "required": ["argv"],
             "additionalProperties": False,
         }),
]

DISPATCH = {"run": run}
