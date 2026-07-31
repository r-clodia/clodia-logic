"""Control-plane interno per lo scambio cifrato agent↔gateway su /shared."""
from __future__ import annotations

import hmac
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..sdk_runtime.session import manager
from ..transfer_crypto import decrypt_file, encrypt_file, public_b64, public_from_b64

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
    root = Path(spawn.scratch).resolve()
    path = Path(value or "").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "path fuori dallo scratch della sessione") from exc
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
