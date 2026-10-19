"""Disabled packs (clodia-platform#563).

A pack is disabled, not removed, by `disabled: true` in its installed manifest
(`DATA/packs/<pack>/pack.yaml`). Files, seeds, memory, datastores and grants
stay where they are; what changes is what reaches the running platform:

- its agents are not loaded (`loader.AgentRegistry.load`): not listed, not
  routed to, not spawned;
- its skills and rules do not reach any agent (`skill_sync`, `rule_sync`),
  from the data catalog or from the image;
- the gateway reads the same flag and treats the pack as absent: connectors
  closed, gateway component unloaded (clodia-tools `connectors.resolved`).

Only a real `true` disables: a string is not a decision. The native agents
are never switched off by a pack's flag — base-pack, which carries them, can
be neither removed nor disabled.

This module only READS the flag (and writes it for the API): it imports
nothing from `server.api`, which imports the agents package.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..config import data_path

LOG = logging.getLogger("agent-server.agents.pack_state")

#: Same set as `api.pack_import._NATIVE_AGENTS` (a test keeps them equal).
NATIVE_AGENTS = frozenset({"clodia", "ophelia"})

#: Packs that can never be disabled (mirror of `NON_REMOVABLE_PACK_NAMES`).
NEVER_DISABLED = frozenset({"base-pack", "logic"})


def _packs_dir() -> Path:
    return data_path("packs")


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — unreadable: not a decision to disable
        return {}
    return doc if isinstance(doc, dict) else {}


def disabled_packs() -> dict[str, dict[str, Any]]:
    """`{pack: manifest}` of every disabled pack."""
    out: dict[str, dict[str, Any]] = {}
    root = _packs_dir()
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/pack.yaml")):
        name = path.parent.name
        if name in NEVER_DISABLED:
            continue
        doc = _read(path)
        if doc.get("disabled") is True:
            out[name] = doc
    return out


def is_disabled(pack: str) -> bool:
    if pack in NEVER_DISABLED:
        return False
    return _read(_packs_dir() / pack / "pack.yaml").get("disabled") is True


def disabled_agents() -> set[str]:
    """The (non-native) agents of the disabled packs."""
    out: set[str] = set()
    for doc in disabled_packs().values():
        out.update(str(a) for a in (doc.get("agents") or []))
    return out - NATIVE_AGENTS


def disabled_plugins() -> set[str]:
    """The plugins of the disabled packs, and the packs' own names (the data
    catalog files a pack's skills under its plugin's name, usually the same)."""
    out: set[str] = set()
    for name, doc in disabled_packs().items():
        out.add(name)
        out.update(str(p) for p in (doc.get("plugins") or []))
    return out - NEVER_DISABLED


def _atomic_write(path: Path, text: str) -> None:
    """Temp file + `os.replace`: a reader (the gateway polls these files)
    never sees a half-written manifest."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pack.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)      # mkstemp makes 0600; the gateway reads these
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def set_disabled(pack: str, disabled: bool) -> None:
    """Write the flag in the installed manifest. KeyError if not installed,
    ValueError for a pack that can never be disabled or whose manifest does
    not parse (refused rather than rewritten as `{disabled: true}`, which
    would wipe its agents, plugins and upstream)."""
    if pack in NEVER_DISABLED:
        raise ValueError(f"'{pack}' non si può disattivare")
    path = _packs_dir() / pack / "pack.yaml"
    if not path.is_file():
        raise KeyError(pack)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"manifest di '{pack}' illeggibile: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"manifest di '{pack}' non è una mappa")
    if disabled:
        doc["disabled"] = True
    else:
        doc.pop("disabled", None)
    _atomic_write(path, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))


# What a disable unmounted, so the enable mounts back exactly that (#563):
# never what the pack's own files declare, which a third-party pack wrote.

def _state_path(pack: str) -> Path:
    return data_path("packs-state") / f"{pack}.json"


def record_unmounted(pack: str, servers: dict[str, str]) -> None:
    """`servers`: `{name: sha256 of its config}` as it was when unmounted."""
    p = _state_path(pack)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        prev = json.loads(p.read_text(encoding="utf-8")).get("unmounted") or {}
        prev = prev if isinstance(prev, dict) else {}
    except Exception:  # noqa: BLE001
        prev = {}
    _atomic_write(p, json.dumps({"unmounted": {**prev, **servers}}, sort_keys=True))


def take_unmounted(pack: str) -> dict[str, str]:
    """The servers to mount back with their config hash, consumed."""
    p = _state_path(pack)
    try:
        out = json.loads(p.read_text(encoding="utf-8")).get("unmounted") or {}
        out = out if isinstance(out, dict) else {}
    except Exception:  # noqa: BLE001
        out = {}
    p.unlink(missing_ok=True)
    return {str(k): str(v) for k, v in out.items()}


def config_hash(config: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()
