"""Store dei Chat Hook (F1).

Un *hook* è una capability opaca legata a UNA chat (topic/DM): chi conosce il
segreto può iniettare un messaggio in quella chat via `POST /hooks/{id}`. Il
segreto si mostra UNA volta alla creazione; a riposo se ne tiene solo l'hash
(sha256). Persistito sotto CLODIA_DATA/hooks/hooks.json.

F1 = solo bearer (segreto). L'autorità del messaggio iniettato è *non fidata*
(vedi api.py); la firma con identità CA (autorità piena) arriva in F2.
"""
from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
from datetime import datetime, timezone
from pathlib import Path

from ..config import data_path

_DIR: Path = data_path("hooks")
_FILE: Path = _DIR / "hooks.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _load() -> list[dict]:
    try:
        return json.loads(_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 — file corrotto: non perdere il servizio
        return []


def _save(rows: list[dict]) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(_FILE)


def _public(row: dict) -> dict:
    """Vista senza segreti (per la UI)."""
    return {k: v for k, v in row.items() if k != "secret_hash"}


def create(tier: str, name: str, label: str, created_by: str,
           author: str | None = None, trigger_agent: str | None = None) -> tuple[dict, str]:
    """Crea (o RIGENERA) l'hook della chat (tier/name). Ritorna (vista_pubblica,
    segreto_in_chiaro). Un topic ha UN SOLO hook: eventuali hook preesistenti per
    quella chat vengono rimossi (rotazione del segreto). Il segreto NON è più
    recuperabile dopo: mostralo all'utente una sola volta."""
    rows = [r for r in _load() if not (r["tier"] == tier and r["name"] == name)]
    hid = pysecrets.token_urlsafe(9)
    while any(r["id"] == hid for r in rows):
        hid = pysecrets.token_urlsafe(9)
    secret = pysecrets.token_urlsafe(24)
    lbl = (label or "hook").strip()[:60]
    row = {
        "id": hid,
        "tier": tier,
        "name": name,
        "label": lbl,
        "author": (author or f"hook:{lbl}").strip()[:80],
        "trigger_agent": (trigger_agent or None),
        "secret_hash": _hash(secret),
        "enabled": True,
        "created_by": created_by,
        "created_at": _now(),
        "last_used": None,
        "last_source": None,
        "uses": 0,
    }
    rows.append(row)
    _save(rows)
    return _public(row), secret


def list_for_chat(tier: str, name: str) -> list[dict]:
    return [_public(r) for r in _load() if r["tier"] == tier and r["name"] == name]


def get(hid: str) -> dict | None:
    """Riga INTERNA (include secret_hash). Uso ingress/authz."""
    return next((r for r in _load() if r["id"] == hid), None)


def verify_secret(hid: str, provided: str) -> dict | None:
    """Ritorna la riga se l'hook esiste, è abilitato e il segreto combacia
    (confronto costante-tempo). Altrimenti None."""
    row = get(hid)
    if not row or not row.get("enabled"):
        return None
    import hmac
    if not provided or not hmac.compare_digest(_hash(provided), row.get("secret_hash", "")):
        return None
    return row


def revoke(hid: str) -> bool:
    rows = _load()
    for r in rows:
        if r["id"] == hid:
            r["enabled"] = False
            _save(rows)
            return True
    return False


def delete(hid: str) -> bool:
    rows = _load()
    new = [r for r in rows if r["id"] != hid]
    if len(new) == len(rows):
        return False
    _save(new)
    return True


def touch(hid: str, source: str | None) -> None:
    rows = _load()
    for r in rows:
        if r["id"] == hid:
            r["last_used"] = _now()
            r["last_source"] = source
            r["uses"] = int(r.get("uses", 0)) + 1
            _save(rows)
            return
