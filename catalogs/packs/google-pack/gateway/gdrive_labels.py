"""Etichette leggibili per le voci `gdrive:folder/<id>` delle whitelist
(clodia-platform#424).

`gdrive:folder/1xxBOdhf4Vz2lgHAsxIBOEfyY9ymJn_NT` è la forma canonica — il
segmento di tipo distingue una cartella da un documento — ma per chi legge la
lista è un codice. Qui si aggiunge, accanto alla voce e senza toccarla:

- `url`: il link per aprirla nel browser, costruito dall'id (sempre presente);
- `name`: il nome della cartella, letto da Drive con la credenziale che ha solo
  il gateway.

Il nome è un aiuto, non un requisito, e la lista non deve mai aspettare Drive:
- cache in memoria (trovati per ore, non trovati per minuti);
- la risoluzione ha un tetto di tempo per chiamata (`BUDGET_S`): ciò che non
  arriva in tempo esce col solo link e continua a risolversi in background,
  così il caricamento successivo lo trova in cache;
- un errore di Drive (cartella non condivisa, account senza accesso, rete) non
  si propaga: si torna al link.
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import re
import threading
import time

LOG = logging.getLogger("clodia-tools.component.google-pack.gdrive_labels")

_FOLDER_RE = re.compile(r"^gdrive:folder/([A-Za-z0-9_-]{10,})$")

#: Attesa massima di una vista per i nomi non ancora in cache.
BUDGET_S = 3.0
_TTL_HIT = 6 * 3600
_TTL_MISS = 10 * 60

_cache: dict[str, tuple[float, str | None]] = {}
_lock = threading.Lock()
_pool = _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="gdrive-label")
_inflight: dict[str, _cf.Future] = {}


def folder_id(uri: str) -> str | None:
    m = _FOLDER_RE.match(str(uri or "").strip())
    return m.group(1) if m else None


def folder_url(fid: str) -> str:
    return f"https://drive.google.com/drive/folders/{fid}"


def _cached(fid: str) -> tuple[bool, str | None]:
    with _lock:
        v = _cache.get(fid)
    if not v:
        return False, None
    scade, nome = v
    if time.time() > scade:
        return False, None
    return True, nome


def _store(fid: str, nome: str | None) -> None:
    with _lock:
        _cache[fid] = (time.time() + (_TTL_HIT if nome else _TTL_MISS), nome)
        _inflight.pop(fid, None)


def _lookup(fid: str) -> str | None:
    """Il nome della cartella, provando ogni account Workspace del vault: la
    cartella può essere condivisa con uno solo di essi."""
    from . import gdrive
    nome = None
    try:
        accounts = gdrive.gworkspace_accounts()
    except Exception:  # noqa: BLE001
        accounts = []
    for acct in accounts:
        try:
            from . import google_io
            f = google_io.call(acct, "drive", "v3", "GET files/{fileId}", params={
                "fileId": fid, "fields": "id,name,mimeType", "supportsAllDrives": True})
            nome = (f or {}).get("name") or None
            if nome:
                break
        except Exception as e:  # noqa: BLE001 — un account senza accesso non è un errore
            LOG.debug("etichetta %s non leggibile con %s: %s", fid, acct, str(e)[:120])
    _store(fid, nome)
    return nome


def _future(fid: str) -> _cf.Future:
    with _lock:
        f = _inflight.get(fid)
        if f is None:
            f = _pool.submit(_lookup, fid)
            _inflight[fid] = f
    return f


def labels(uris: list[str], budget_s: float = BUDGET_S) -> dict[str, dict]:
    """`{uri: {"url": …, "name": … | None}}` per le sole voci `gdrive:folder/`.

    Non solleva mai. Le voci già in cache escono subito; le altre aspettano al
    più `budget_s` in tutto, poi escono col solo link.
    """
    out: dict[str, dict] = {}
    attese: dict[str, tuple[str, _cf.Future]] = {}
    for u in uris or []:
        fid = folder_id(u)
        if not fid:
            continue
        out[u] = {"url": folder_url(fid), "name": None}
        trovato, nome = _cached(fid)
        if trovato:
            out[u]["name"] = nome
        else:
            attese[u] = (fid, _future(fid))
    if attese:
        _cf.wait([f for _fid, f in attese.values()], timeout=max(0.0, budget_s))
        for u, (_fid, f) in attese.items():
            if f.done():
                try:
                    out[u]["name"] = f.result()
                except Exception:  # noqa: BLE001
                    pass
    return out
