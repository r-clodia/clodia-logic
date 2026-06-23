"""Bootstrap & Admin Auth — F1: stato dell'istanza (claim).

Spec "Bootstrap & Admin Auth". Al primo setup l'istanza è UNINITIALIZED: tutto
inerte tranne la creazione di un agente (il popup "nuovo agente" resta usabile).
Creando il PRIMO principal `type: human` lo si elegge **superadmin** e l'istanza
diventa INITIALIZED — vedi `create_agent` in agent_registry (genera il cert dalla
pubkey del browser). Qui esponiamo solo lo STATO; la creazione passa dal popup.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter

from ..config import data_path

router = APIRouter(prefix="/api/admin", tags=["admin"])

AGENTS_DIR = data_path("agents")
_ADMIN_ROLES = ("superadmin", "admin")


def _is_admin_yaml(p: Path) -> bool:
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    return d.get("type") == "human" and d.get("role") in _ADMIN_ROLES


def admins() -> list[dict]:
    out: list[dict] = []
    if AGENTS_DIR.is_dir():
        for d in sorted(AGENTS_DIR.iterdir()):
            ay = d / "agent.yaml"
            if ay.is_file() and _is_admin_yaml(ay):
                meta = yaml.safe_load(ay.read_text(encoding="utf-8")) or {}
                out.append({"name": d.name, "display_name": meta.get("display_name"),
                            "role": meta.get("role")})
    return out


def is_initialized() -> bool:
    """True se esiste almeno un admin (human/superadmin) → istanza reclamata."""
    return len(admins()) > 0


def is_admin(name: str | None) -> bool:
    """True se il principal è un human con ruolo admin/superadmin."""
    if not name:
        return False
    return _is_admin_yaml(AGENTS_DIR / name / "agent.yaml")


@router.get("/state")
async def state() -> dict:
    """Stato del bootstrap (aperto: serve alla webui pre-claim)."""
    return {"initialized": is_initialized(), "admins": [a["name"] for a in admins()]}
