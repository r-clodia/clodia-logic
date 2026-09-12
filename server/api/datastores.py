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
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

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


def _active_rag_collections() -> list[dict[str, Any]]:
    out = []
    for pack in list_plugins():
        for rc in pack.get("rag_collections") or []:
            out.append({"name": rc.get("name"), "description": rc.get("description", ""),
                        "tier": rc.get("tier", "SEAL-0"), "pack": pack["name"],
                        "status": "active"})
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


async def _orphaned_rag_collections() -> list[dict[str, Any]]:
    dichiarate = _active_rag_names()
    try:
        tutte = await rag_store.list_collections_async()
    except rag_store.RagStoreError as e:
        LOG.warning("datastores: inventario RAG non disponibile (%s)", e)
        return []
    return [{"name": c.get("collection"), "tier": c.get("tier", "SEAL-0"),
            "documents": c.get("documents", 0), "chunks": c.get("chunks", 0),
            "pack": None, "status": "orphaned"}
           for c in tutte if c.get("collection") not in dichiarate]


@router.get("/clodia/datastores")
async def list_datastores() -> dict[str, Any]:
    return {
        "datastores": _active_datastores() + _archived_datastores(),
        "rag_collections": _active_rag_collections() + await _orphaned_rag_collections(),
    }


# ── Dentro una riga: tabelle, pagine di righe, documenti (#342) ──────────────
#
# L'inventario sopra dice che un dato ESISTE; queste rotte lo fanno vedere.
#
# Nessun SQLite aperto qui, e non per pigrizia: il gateway ha già la
# connessione `mode=ro`, la whitelist per tipo di istruzione, il tetto in byte
# e l'audit (`clodia-tools/server/tools/datastore_sql.py`), e sopra di essi il
# PDP che decide se QUESTA persona può leggere QUEL datastore
# (`_datastore_authorize`, ramo umano — clodia-tools#275). Riaprire il file da
# questo lato significherebbe riscrivere quelle quattro cose e tenerle
# allineate a mano: la seconda copia diverge, ed è la copia senza audit.
#
# `forward` NON è `require_authz`: non chiede solo il permesso, inoltra
# l'esecuzione. L'autorizzazione qui non è un controllo in più da ricordarsi —
# è la stessa chiamata che porta i dati.

#: Righe per pagina: tetto, non default del client. Il gateway ha già un tetto
#: in BYTE (32 KiB) che taglia a metà elenco; questo è il tetto in RIGHE, che è
#: l'unità in cui ragiona una tabella in UI.
_MAX_ROWS = 200
_DEFAULT_ROWS = 50

#: Le tabelle vere del datastore. `sqlite_master` e non `PRAGMA table_list`:
#: è una SELECT (quindi passa la whitelist del gateway senza eccezioni), esiste
#: in ogni versione di SQLite, e il filtro sugli oggetti interni lo fa il
#: motore invece del chiamante.
_TABLES_SQL = ("SELECT name, type FROM sqlite_master "
               "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
               "ORDER BY name")


async def _read(request: Request, key: str, query: str, params: list) -> dict:
    """Una lettura sul datastore `<pack>/<nome>`, eseguita dal gateway."""
    return await gateway_pdp.forward_async(
        request, "datastore.read",
        {"datastore": key, "query": query, "params": params})


async def _table_names(request: Request, key: str) -> list[str]:
    res = await _read(request, key, _TABLES_SQL, [])
    return [r.get("name") for r in (res or {}).get("rows", []) if r.get("name")]


@router.get("/clodia/datastores/{pack}/{name}/tables")
async def datastore_tables(pack: str, name: str, request: Request) -> dict[str, Any]:
    key = f"{pack}/{name}"
    return {"datastore": key, "tables": await _table_names(request, key)}


@router.get("/clodia/datastores/{pack}/{name}/rows")
async def datastore_rows(pack: str, name: str, request: Request, table: str,
                         limit: int = _DEFAULT_ROWS, offset: int = 0) -> dict[str, Any]:
    """Una pagina di righe di UNA tabella del datastore.

    `table` è un IDENTIFICATORE: in SQL non è parametrizzabile, quindi l'unica
    difesa possibile è non costruire l'istruzione affatto se il nome non è fra
    quelli che il datastore dichiara. La lista arriva da `sqlite_master` del
    datastore stesso, non dal client, e il confronto è di uguaglianza — niente
    normalizzazioni che riaprirebbero la porta appena chiusa.
    """
    key = f"{pack}/{name}"
    tabelle = await _table_names(request, key)
    if table not in tabelle:
        raise HTTPException(
            400, f"tabella '{table}' non presente in '{key}'. "
                 f"Tabelle disponibili: {', '.join(tabelle) or 'nessuna'}")
    limit = max(1, min(int(limit), _MAX_ROWS))
    offset = max(0, int(offset))
    # UNA riga in più di quelle che si mostrano: dice se esiste una pagina
    # successiva senza un `count(*)` su una tabella che può essere grande. La
    # riga in più serve a sapere, non a essere mostrata.
    res = await _read(
        request, key,
        f'SELECT * FROM "{table.replace(chr(34), chr(34) * 2)}" LIMIT ? OFFSET ?',
        [limit + 1, offset])
    righe = (res or {}).get("rows", [])
    return {"datastore": key, "table": table,
            "columns": (res or {}).get("columns", []),
            "rows": righe[:limit], "limit": limit, "offset": offset,
            "has_more": len(righe) > limit,
            # Il taglio in BYTE del gateway è una cosa diversa dalla fine della
            # pagina: se la riga è enorme, la pagina può finire prima del limite
            # richiesto. Chi mostra la tabella deve poterlo dire.
            "truncated": bool((res or {}).get("truncated"))}


@router.get("/clodia/datastores/rag/{collection}/documents")
async def rag_collection_documents(collection: str, request: Request) -> dict[str, Any]:
    """I documenti INIETTATI in una collection: l'inventario ne dava il
    conteggio, non i nomi — «12 documenti» non si amministra.

    `require_authz` e non `forward`: la lettura non è un verbo eseguibile dal
    gateway per conto di questa persona (`rag.list` passa dai grant dell'AGENTE
    chiamante), ed è lo stesso motivo per cui l'inventario delle collection
    vive su una rotta interna privilegiata. La decisione resta del PDP, la
    lettura la fa il runner — come già fa `_orphaned_rag_collections`.
    """
    await gateway_pdp.require_authz_async(request, "rag.list")
    try:
        documenti = await rag_store.list_documents_async(collection)
    except rag_store.RagStoreError as e:
        # 503 e non una lista vuota: «non ho potuto sapere» e «non c'è niente»
        # sono due affermazioni diverse, e la seconda qui sarebbe falsa.
        # L'inventario può degradare (là il resto della pagina resta leggibile);
        # il contenuto di UNA collection chiesto apposta, no.
        LOG.warning("datastores: documenti di '%s' non disponibili (%s)", collection, e)
        raise HTTPException(
            503, f"documenti della collection '{collection}' non disponibili: {e}. "
                 "Non è un problema di permessi — riprova fra qualche secondo.") from e
    return {"collection": collection, "documents": documenti}


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
