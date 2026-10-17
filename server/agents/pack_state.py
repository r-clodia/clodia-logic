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

import logging
from pathlib import Path
from typing import Any

import yaml

from ..config import data_path

LOG = logging.getLogger("agent-server.agents.pack_state")

#: Same set as `api.pack_import._NATIVE_AGENTS` (a test keeps them equal).
NATIVE_AGENTS = frozenset({"clodia", "ophelia", "messaggero"})

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


def set_disabled(pack: str, disabled: bool) -> None:
    """Write the flag in the installed manifest. KeyError if not installed,
    ValueError for a pack that can never be disabled."""
    if pack in NEVER_DISABLED:
        raise ValueError(f"'{pack}' non si può disattivare")
    path = _packs_dir() / pack / "pack.yaml"
    if not path.is_file():
        raise KeyError(pack)
    doc = _read(path)
    if disabled:
        doc["disabled"] = True
    else:
        doc.pop("disabled", None)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
