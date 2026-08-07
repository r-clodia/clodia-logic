"""Store dei Chat Hook.

Un *hook* è legato a UNA chat (topic/DM): il suo id è lo slug globale del topic
e chi conosce il segreto può iniettare un messaggio via `POST /hooks/{id}`. Il
segreto si mostra UNA volta alla creazione; a riposo se ne tiene solo l'hash
(sha256). Persistito sotto CLODIA_DATA/hooks/hooks.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets as pysecrets
from datetime import datetime, timezone
from pathlib import Path

from ..config import data_path

LOG = logging.getLogger("agent-server.hooks.db")

_DIR: Path = data_path("hooks")
_FILE: Path = _DIR / "hooks.json"


class HookConflictError(RuntimeError):
    """Lo slug è già associato a un topic in un altro tier."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _load() -> list[dict]:
    try:
        rows = json.loads(_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(rows, list):
        raise ValueError("hooks.json deve contenere una lista")

    # Migrazione one-shot dal vecchio id opaco allo slug. Non scegliamo mai quale
    # collisione conservare: il duplicato deve essere risolto esplicitamente.
    changed = False
    owners: dict[str, tuple[str, str]] = {}
    for row in rows:
        slug = str(row.get("name") or "")
        topic = (str(row.get("tier") or ""), slug)
        previous = owners.get(slug)
        if previous and previous != topic:
            raise HookConflictError(
                f"slug hook globale duplicato '{slug}': "
                f"{previous[0]}/{previous[1]} e {topic[0]}/{topic[1]}")
        owners[slug] = topic
        if row.get("id") != slug:
            row["id"] = slug
            changed = True
        if "trigger_agent" in row:
            row.pop("trigger_agent", None)
            changed = True
    if changed:
        _save(rows)
    return rows


def _save(rows: list[dict]) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(_FILE)


def _public(row: dict) -> dict:
    """Vista senza segreti (per la UI)."""
    return {k: v for k, v in row.items() if k != "secret_hash"}


def _topic_exists(tier: str, name: str) -> bool:
    """Il topic esiste davvero? Su errore risponde True — FAIL CLOSED.

    Un gateway irraggiungibile non deve trasformarsi in «la riga è fantasma,
    rimuovila»: sarebbe un modo per cancellare hook veri durante un riavvio.
    """
    try:
        from ..api import topics_client
        return topics_client.open_topic(tier, name) is not None
    except Exception:  # noqa: BLE001
        return True


def create(tier: str, name: str, label: str, created_by: str,
           author: str | None = None,
           rate_per_min: int = 30) -> tuple[dict, str]:
    """Crea (o RIGENERA) l'hook della chat (tier/name). Ritorna (vista_pubblica,
    segreto_in_chiaro). Un topic ha UN SOLO hook: eventuali hook preesistenti per
    quella chat vengono rimossi (rotazione del segreto). Il segreto NON è più
    recuperabile dopo: mostralo all'utente una sola volta."""
    all_rows = _load()
    conflict = next(
        (r for r in all_rows if r["name"] == name and r["tier"] != tier), None)
    if conflict and not _topic_exists(conflict["tier"], conflict["name"]):
        # RIGA FANTASMA. Il topic è cambiato di tier e questo registro non l'ha
        # seguito: la riga punta a un posto che non esiste più e blocca il topic
        # vero. Successo il 7 ago 2026 su `proof-of-flex-2`, passato da SEAL-2 a
        # SEAL-1: creare il suo hook rispondeva 409 citando un topic assente dal
        # disco.
        #
        # Si ripara invece di rifiutare, e si ripara QUI e non con una migrazione
        # perché il disallineamento può ripresentarsi a ogni cambio di tier —
        # nulla, oggi, tiene aggiornato questo registro quando un topic si sposta.
        # Un controllo che si auto-guarisce non ha bisogno che qualcuno se ne
        # ricordi.
        LOG.warning("hook: riga fantasma %s/%s rimossa (il topic non esiste); "
                    "lo slug torna disponibile per %s/%s",
                    conflict["tier"], conflict["name"], tier, name)
        all_rows = [r for r in all_rows
                    if not (r["name"] == conflict["name"]
                            and r["tier"] == conflict["tier"])]
        _save(all_rows)
        conflict = None
    if conflict:
        raise HookConflictError(
            f"slug '{name}' già usato da {conflict['tier']}/{conflict['name']}")
    rows = [r for r in all_rows if not (r["tier"] == tier and r["name"] == name)]
    secret = pysecrets.token_urlsafe(24)
    lbl = (label or "hook").strip()[:60]
    row = {
        "id": name,
        "tier": tier,
        "name": name,
        "label": lbl,
        "author": (author or f"hook:{lbl}").strip()[:80],
        "secret_hash": _hash(secret),
        "enabled": True,
        "created_by": created_by,
        "created_at": _now(),
        "last_used": None,
        "last_source": None,
        "uses": 0,
        "rate_per_min": int(rate_per_min),
        "events": [],   # audit-log per hook (ultimi N ingress)
    }
    rows.append(row)
    _save(rows)
    return _public(row), secret


def ensure(tier: str, name: str, label: str, created_by: str,
           rate_per_min: int = 30) -> tuple[dict, str | None]:
    """Assicura l'hook automatico senza ruotare un segreto già esistente."""
    rows = _load()
    existing = next(
        (r for r in rows if r["tier"] == tier and r["name"] == name), None)
    if existing:
        return _public(existing), None
    return create(tier, name, label, created_by, rate_per_min=rate_per_min)


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


_EVENTS_CAP = 20


def record_event(hid: str, status: str, source: str | None = None,
                 authority: str | None = None, principal: str | None = None,
                 note: str | None = None) -> None:
    """Appende un evento all'audit-log dell'hook (cap _EVENTS_CAP). Se status=='ok'
    aggiorna anche last_used/last_source/uses."""
    rows = _load()
    for r in rows:
        if r["id"] == hid:
            ev = {"ts": _now(), "status": status, "source": source,
                  "authority": authority, "principal": principal, "note": note}
            evs = r.get("events") or []
            evs.append(ev)
            r["events"] = evs[-_EVENTS_CAP:]
            if status == "ok":
                r["last_used"] = ev["ts"]
                r["last_source"] = source
                r["uses"] = int(r.get("uses", 0)) + 1
            _save(rows)
            return
