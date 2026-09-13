"""Inventario di datastore (SQLite/file) e collection RAG dei pack — pagina
"Databases" della webui.

Davide, 9 set 2026: «riprendere il concetto di dati (db, vector db)
indipendenti dai topic». Esistevano già due primitive nel manifest di un pack
(`datastores:`, `rag_collections:`, viste in `plugins.py::_plugin_item`), ma
nessun punto le elencava tutte insieme, né un modo di ripulire ciò che resta
ORFANO quando un pack viene disinstallato — comportamento voluto:
disinstallare non cancella mai i dati (`pack_deprovision.py`), la rimozione
definitiva resta un atto umano.

Tre stati per una riga:
- `active`   — il pack che la dichiara è installato;
- `archived` — SOLO i datastore: la cartella del pack rimosso è stata spostata
  in `plugins-archive/<nome>-<timestamp>/` (`plugin_import.archive_plugin_dir`)
  e può essere cancellata definitivamente da qui;
- `orphaned` — SOLO le collection RAG: il pack che le dichiarava non è più
  installato, ma la collection resta viva in pgvector (nessuna azione da qui:
  il servizio `eu-rag-search` non espone un endpoint di cancellazione).

Ogni riga porta anche la propria MEMBER LIST (clodia-platform#341: «clearance e
lista di seed autorizzati»), e le due primitive la tengono in due posti diversi:
un datastore la dichiara nel manifest (`seeds`, proiettato da `plugins.py`), una
collection RAG no — lì la lista esiste girata, nei grant `rag_read`/`rag_write`
dei seed, e la si legge per collection con `agents/rag_members.py`.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..agents import rag_members
from . import gateway_pdp, plugin_import, rag_store
from .plugins import list_plugins

LOG = logging.getLogger("agent-server.api.datastores")
router = APIRouter()

#: `<nome-pack>-<YYYYMMDD-HHMMSS>`, lo stesso formato scritto da
#: `plugin_import.move_aside`. Il nome del pack può contenere trattini (es.
#: "studio-legale"): si ancora sul suffisso timestamp, non sul primo `-`.
_ARCHIVE_DIR_RE = re.compile(r"^(?P<pack>.+)-(?P<ts>\d{8}-\d{6})$")


def _active_datastores() -> list[dict[str, Any]]:
    out = []
    for pack in list_plugins():
        for ds in pack.get("datastores") or []:
            out.append({**ds, "pack": pack["name"], "status": "active"})
    return out


def _active_rag_names() -> set[str]:
    names: set[str] = set()
    for pack in list_plugins():
        for rc in pack.get("rag_collections") or []:
            if rc.get("name"):
                names.add(rc["name"])
    return names


def _active_rag_collections(membri: rag_members.RagMembership) -> list[dict[str, Any]]:
    out = []
    for pack in list_plugins():
        for rc in pack.get("rag_collections") or []:
            out.append({"name": rc.get("name"), "description": rc.get("description", ""),
                        "tier": rc.get("tier", "SEAL-0"), "pack": pack["name"],
                        "status": "active", **membri.of(rc.get("name"))})
    return out


def _archived_datastores() -> list[dict[str, Any]]:
    """I datastore già spostati in `plugins-archive/` da un pack rimosso.

    Legge `plugin.yaml` DENTRO la cartella archiviata: `move_aside` sposta
    l'intera directory, manifest compreso, quindi la dichiarazione originale
    dei datastore sopravvive lì — nessun indice separato da mantenere.
    """
    root = plugin_import._archive_root()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        m = _ARCHIVE_DIR_RE.match(entry.name)
        pack = m.group("pack") if m else entry.name
        ts = m.group("ts") if m else ""
        manifest = entry / "plugin.yaml"
        declared: list[dict[str, Any]] = []
        if manifest.is_file():
            try:
                import yaml
                meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
                declared = plugin_import._sanitize_datastores(
                    meta.get("datastores") if isinstance(meta, dict) else None)
            except Exception as e:  # noqa: BLE001 — un manifest illeggibile non deve rompere l'elenco
                LOG.warning("datastores: manifest archiviato di '%s' illeggibile (%s)",
                           entry.name, str(e)[:120])
        if declared:
            for ds in declared:
                out.append({**ds, "pack": pack, "status": "archived",
                           "archive_dir": entry.name, "archived_at": ts})
        else:
            # Manifest assente/illeggibile o senza `datastores:` dichiarati:
            # l'intera cartella è comunque dato salvato (stesso principio del
            # ramo "undeclared" in `archive_plugin_dir`) — una riga sola che
            # copre tutto, invece di ometterla.
            out.append({"path": ".", "pack": pack, "status": "archived",
                       "archive_dir": entry.name, "archived_at": ts,
                       "pii": None, "purpose": "cartella intera (datastore non dichiarati)"})
    return out


async def _orphaned_rag_collections(membri: rag_members.RagMembership) -> list[dict[str, Any]]:
    dichiarate = _active_rag_names()
    try:
        tutte = await rag_store.list_collections_async()
    except rag_store.RagStoreError as e:
        LOG.warning("datastores: inventario RAG non disponibile (%s)", e)
        return []
    return [{"name": c.get("collection"), "tier": c.get("tier", "SEAL-0"),
            "documents": c.get("documents", 0), "chunks": c.get("chunks", 0),
            "pack": None, "status": "orphaned", **membri.of(c.get("collection"))}
           for c in tutte if c.get("collection") not in dichiarate]


@router.get("/clodia/datastores")
async def list_datastores() -> dict[str, Any]:
    # Member list girata UNA volta per richiesta e passata alle due metà: è una
    # scansione della registry dei seed, e attive e orfane devono comunque
    # rispondere con lo stesso indice.
    membri = rag_members.build()
    return {
        "datastores": _active_datastores() + _archived_datastores(),
        "rag_collections": _active_rag_collections(membri)
                           + await _orphaned_rag_collections(membri),
    }


def _safe_archive_dir(name: str) -> Path | None:
    """`name` deve essere ESATTAMENTE un componente diretto di `plugins-archive/`
    nella forma attesa — niente `/`, niente `..`, niente path assoluto. Un
    archive_dir non valido non deve poter uscire da quella cartella."""
    if not name or "/" in name or "\\" in name or not _ARCHIVE_DIR_RE.match(name):
        return None
    root = plugin_import._archive_root()
    candidate = root / name
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


@router.delete("/clodia/datastores/archived/{archive_dir}")
async def purge_archived_datastore(archive_dir: str, request: Request):
    """Cancella DEFINITIVAMENTE una cartella già in `plugins-archive/`.

    Unica azione distruttiva di questa API: `datastores.purge`, owner-only
    come le altre azioni distruttive dei pack (`plugins.py::delete_plugin`).
    """
    await gateway_pdp.require_authz_async(request, "datastores.purge")
    path = _safe_archive_dir(archive_dir)
    if path is None:
        return JSONResponse(status_code=400, content={"error": "archive_dir non valido"})
    if not path.is_dir():
        return JSONResponse(status_code=404, content={"error": "cartella non trovata"})
    shutil.rmtree(path)
    LOG.info("datastores: purge definitivo di %s", archive_dir)
    return {"ok": True, "archive_dir": archive_dir}
