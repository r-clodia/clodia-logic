"""Runtime configs that carry a spawn's MCP token, kept off the command line.

clodia-platform#706: the Claude SDK turned `mcp_servers={...}` into
`--mcp-config '{..."Authorization":"Bearer ckt1..."}'` on the CLI's argv, and
opencode started `mcp-remote --header "Authorization: Bearer ckt1..."`. The
argv of every process is world-readable (`/proc/<pid>/cmdline`, `ps auxww`), so
any spawn with a shell could read every other live spawn's signed token and
call the gateway as that agent (higher clearance, other rooms) until it expired.

The token now lives in a file of the spawn's own: mode 0600, owned by the
spawn's sandbox uid, inside the spawn directory (itself 0700 to that uid).
What reaches argv is a path, never the token. `/proc/<pid>/environ` is not the
same exposure: the kernel lets only the same uid (or root) read it, and with the
sandbox on each spawn has its own uid (`session._alloc_uid`).

Without the sandbox (`CLODIA_AGENT_SANDBOX_UID` unset) every runtime runs as
root and can read every file anyway; the file is then root-owned 0600, which
still keeps the token out of `ps`.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

#: The file the Claude CLI reads through `--mcp-config <path>`.
MCP_CONFIG_NAME = ".clodia-mcp.json"


def write_private(path: Path, content: str, *, uid: Optional[int] = None,
                  gid: Optional[int] = None) -> Path:
    """Write `content` to `path` atomically, mode 0600, owned by `uid:gid`.

    The directory may belong to the agent (its spawn dir), so nothing here
    follows a name the agent could have planted: the temporary file is created
    with O_EXCL|O_NOFOLLOW (`mkstemp`), ownership and mode are set on the open
    descriptor, and `os.replace` swaps the directory entry — a symlink left at
    `path` is replaced, never written through.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-",
                               suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            if uid is not None:
                os.fchown(f.fileno(), int(uid), int(gid) if gid is not None else -1)
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_mcp_config(directory: Path, servers: dict, *, uid: Optional[int] = None,
                     gid: Optional[int] = None) -> Path:
    """The Claude CLI's `--mcp-config` file for `servers` (`{"mcpServers": …}`,
    the shape the SDK itself serialises for a dict)."""
    return write_private(Path(directory) / MCP_CONFIG_NAME,
                         json.dumps({"mcpServers": servers}),
                         uid=uid, gid=gid)

