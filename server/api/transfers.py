"""Control-plane interno per lo scambio cifrato agent↔gateway su /shared."""
from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..sdk_runtime.session import manager
from ..transfer_crypto import decrypt_file, encrypt_file, public_b64, public_from_b64

LOG = logging.getLogger("agent-server.transfers")

router = APIRouter(prefix="/internal/transfers", tags=["internal-transfers"])

SHARED_ROOT = Path(os.environ.get("CLODIA_SHARED_ROOT", "/shared"))
EXCHANGES = SHARED_ROOT / "exchanges"
GATEWAY_PUBLIC = SHARED_ROOT / "gateway.pub"
MAX_BYTES = int(os.environ.get("CLODIA_TRANSFER_MAX_BYTES", str(256 * 1024 * 1024)))
TTL_SECONDS = int(os.environ.get("CLODIA_TRANSFER_TTL_SECONDS", "900"))


def _authorize(request: Request) -> None:
    expected = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    got = (request.headers.get("x-orchestrator-secret") or "").strip()
    if not expected or not got or not hmac.compare_digest(expected, got):
        raise HTTPException(401, "internal transfer authentication required")


def _session(chat_id: str):
    try:
        chat = manager.get(chat_id)
    except KeyError as exc:
        raise HTTPException(404, "sessione agent non trovata") from exc
    spawn = getattr(chat, "_spawn", None)
    private = getattr(chat, "_transfer_private", None)
    if spawn is None or private is None:
        raise HTTPException(409, "sessione senza spawn cifrato")
    return chat, spawn, private


def _scratch_path(spawn, value: str) -> Path:
    """Risolve `value` DENTRO lo scratch della sessione.

    Un path relativo si unisce alla radice dello scratch invece di essere
    rifiutato. Prima veniva risolto contro la cwd del processo agent-server, cioè
    fuori dallo scratch per costruzione → 400 su quello che un agente scrive
    naturalmente (`estratto.zip`, `files/x.pdf`).

    Il caso che ci è costato una giornata è peggiore, perché sembra corretto: la
    cwd dell'agente è la RADICE dello spawn, mentre lo scratch è `<spawn>/scratch`.
    Un agente che fa `pwd` e compone un path assoluto finisce ACCANTO allo scratch,
    non dentro — e prende 400 su un path che ha appena letto dal proprio ambiente.
    Tre agenti diversi hanno fallito così, ognuno concludendo che il servizio era
    guasto.
    """
    root = Path(spawn.scratch).resolve()
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(400, f"dest richiesto: un nome file, o un path sotto {root}")
    candidate = Path(raw)
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        # Il motivo va nel log E nella risposta: prima non era in nessuno dei due,
        # e l'agente vedeva "400 Bad Request" senza nulla su cui agire.
        LOG.warning("transfer rifiutato: '%s' non sta sotto lo scratch %s", raw, root)
        raise HTTPException(
            400,
            f"'{raw}' non sta nel tuo scratch. Lo scratch è {root} — NON la tua "
            f"cwd, che è la radice dello spawn (un livello sopra). Passa solo il "
            f"nome del file (es. '{candidate.name or 'file.zip'}') e ci penso io, "
            f"oppure un path sotto {root}.") from exc
    return path


def _exchange_path(exchange_id: str) -> Path:
    try:
        clean = str(uuid.UUID(exchange_id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "exchange_id non valido") from exc
    return EXCHANGES / f"{clean}.clx"


def _cleanup() -> None:
    cutoff = time.time() - TTL_SECONDS
    if not EXCHANGES.is_dir():
        return
    for path in EXCHANGES.glob("*.clx"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


@router.post("/public-key")
async def transfer_public_key(request: Request) -> dict:
    _authorize(request)
    body = await request.json()
    _chat, spawn, private = _session(str(body.get("chat_id") or ""))
    return {"recipient": spawn.dir.name, "public_key": public_b64(private.public_key())}


@router.post("/deliver")
async def transfer_deliver(request: Request) -> dict:
    _authorize(request)
    _cleanup()
    body = await request.json()
    _chat, spawn, private = _session(str(body.get("chat_id") or ""))
    envelope = _exchange_path(str(body.get("exchange_id") or ""))
    dest = _scratch_path(spawn, str(body.get("dest") or ""))
    partial = dest.with_name(dest.name + ".part")
    try:
        header = decrypt_file(envelope, partial, recipient=spawn.dir.name,
                              private_key=private, max_bytes=MAX_BYTES,
                              max_age_seconds=TTL_SECONDS)
        partial.replace(dest)
        return {"local_path": str(dest), "size": header["size"],
                "sha256": header["sha256"]}
    finally:
        partial.unlink(missing_ok=True)
        envelope.unlink(missing_ok=True)


@router.post("/collect")
async def transfer_collect(request: Request) -> dict:
    _authorize(request)
    _cleanup()
    body = await request.json()
    _chat, spawn, _private = _session(str(body.get("chat_id") or ""))
    source = _scratch_path(spawn, str(body.get("src") or ""))
    if not source.is_file():
        raise HTTPException(404, "file sorgente non trovato")
    if source.stat().st_size > MAX_BYTES:
        raise HTTPException(413, f"file oltre il limite di {MAX_BYTES} byte")
    try:
        gateway_key = public_from_b64(GATEWAY_PUBLIC.read_text("ascii").strip())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "chiave pubblica transfer del gateway non disponibile") from exc
    exchange_id = str(uuid.uuid4())
    envelope = _exchange_path(exchange_id)
    header = encrypt_file(source, envelope, recipient="gateway", sender=spawn.dir.name,
                          recipient_key=gateway_key)
    return {"exchange_id": exchange_id, "sender": spawn.dir.name,
            "size": header["size"], "sha256": header["sha256"]}
