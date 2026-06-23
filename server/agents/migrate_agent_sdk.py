"""Migrazione agent.yaml legacy: aggiunge agent_sdk: claude se assente.

Uso:
    python3 -m server.agents.migrate_agent_sdk

Opera sulla datadir configurata (`CLODIA_DATA/agents`). Non legge segreti.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import data_path


AGENTS_DIR = data_path("agents")


def migrate_file(path: Path) -> bool:
    text = path.read_text()
    if re.search(r"^agent_sdk:", text, re.MULTILINE):
        return False
    lines = text.splitlines()
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.startswith("description:"):
            out.append("agent_sdk: claude")
            inserted = True
    if not inserted:
        out.insert(0, "agent_sdk: claude")
    path.write_text("\n".join(out) + "\n")
    return True


def main() -> None:
    changed = []
    if AGENTS_DIR.is_dir():
        for path in sorted(AGENTS_DIR.glob("*/agent.yaml")):
            if migrate_file(path):
                changed.append(path)
    print(f"agent_sdk migration: {len(changed)} file aggiornati")
    for path in changed:
        print(path)


if __name__ == "__main__":
    main()
