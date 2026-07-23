"""API dei PACK: pack = [agent seeds] + [plugins], nessuno obbligatorio.

Gerarchia del catalogo (decisione 4 lug 2026):

    pack   := [agent seeds] + [plugins]
    plugin := [skills] + [rules] + [mcp_servers]     (standard Claude Code)

I plugin possono vivere anche "sciolti" (fuori da qualunque pack): la lista
plugin completa è su `/clodia/plugins`; qui si espongono i pack (aggregati
importati) con i loro agenti e plugin risolti.

Per ogni agente del pack l'API espone lo stato dei `requires_plugins` del suo
agent.yaml: prerequisiti SOFT — plugin mancante → `missing_plugins` (warning
in UI), mai un errore.

L'import è UNIFICATO: `POST /clodia/packs/import[-url]` accetta sia un pack
sia un plugin sciolto (Claude plugin / plugin.yaml / bare skills) e ritorna
`kind: "pack" | "plugin"`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..agents.loader import registry
from ..config import workspace_path
from . import catalog, gateway_pdp, pack_import, plugins as plugins_api

LOG = logging.getLogger("agent-server.api.packs")
router = APIRouter()


def _bundle_catalog_dir(name: str):
    """Path del pack `name` nel catalogo BUNDLED (spedito con clodia-logic) —
    la versione DISPONIBILE per un pack first-party. None se non è bundled."""
    d = workspace_path(f"catalogs/packs/{name}")
    return d if (d / "pack.yaml").is_file() else None


def _bundle_pack_version(name: str) -> str:
    d = _bundle_catalog_dir(name)
    if not d:
        return ""
    try:
        m = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8")) or {}
        return str(m.get("version") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _version_tuple(v: str):
    """SemVer grezzo per confronto: '6.4.0' → (6,4,0). Non-numerico → stringa."""
    try:
        return tuple(int(x) for x in v.split("-")[0].split("."))
    except Exception:  # noqa: BLE001
        return (v,)


# ── Check update / Update da GitHub (Opzione A) ──────────────────────────────
def _pack_upstream(name: str) -> dict | None:
    """`upstream: {repo, path, ref}` dal manifest installato (o dal catalogo
    bundled come fallback). None se il pack non dichiara un upstream."""
    candidates = [pack_import.PACKS_META_DIR / name / "pack.yaml",
                  workspace_path(f"catalogs/packs/{name}/pack.yaml")]
    for src in candidates:
        if not src.is_file():
            continue
        try:
            m = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        up = m.get("upstream")
        if isinstance(up, dict) and up.get("repo"):
            return {"repo": str(up["repo"]).strip(),
                    "path": str(up.get("path") or "").strip().strip("/"),
                    "ref": str(up.get("ref") or "main").strip()}
    return None


def _github_token() -> str | None:
    """PAT dal vault per i repo privati (clodia-packs). None se assente/pubblico."""
    try:
        from . import git_client
        return git_client.read_credential("github_pat")
    except Exception:  # noqa: BLE001
        return None


def _fetch_remote_pack_version(up: dict) -> str:
    """Legge la versione del `pack.yaml` remoto via GitHub API contents (funziona
    per repo pubblici e privati, token in header, mai nell'URL)."""
    import base64 as _b64
    import httpx
    path = (up["path"] + "/pack.yaml").lstrip("/") if up["path"] else "pack.yaml"
    url = f"https://api.github.com/repos/{up['repo']}/contents/{path}"
    headers = {"Accept": "application/vnd.github.raw+json"}
    tok = _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = httpx.get(url, params={"ref": up["ref"]}, headers=headers, timeout=12.0)
    r.raise_for_status()
    # con Accept raw → corpo = contenuto del file; senza → JSON {content: b64}
    text = r.text
    if text.lstrip().startswith("{"):
        text = _b64.b64decode(r.json().get("content") or "").decode("utf-8")
    m = yaml.safe_load(text) or {}
    return str(m.get("version") or "").strip()


def _load_pack_manifest(path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        LOG.warning("pack.yaml %s non leggibile: %s", path, e)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _agent_entry(name: str, installed_plugins: set[str]) -> dict[str, Any]:
    """Stato di un agente del pack: installato? prerequisiti plugin soddisfatti?"""
    spec = registry.get_by_name(name)
    if spec is None:
        return {
            "name": name,
            "installed": False,
            "description": "",
            "requires_plugins": [],
            "missing_plugins": [],
        }
    requires = [
        {"name": r.name, "hard": r.hard} for r in (spec.requires_plugins or [])
    ]
    missing = [r["name"] for r in requires if r["name"] not in installed_plugins]
    return {
        "name": name,
        "installed": True,
        "description": (spec.description or "").strip(),
        "requires_plugins": requires,
        "missing_plugins": missing,
    }


def _pack_provider_info(manifest: dict[str, Any]) -> dict[str, Any]:
    """Provider dichiarati dal pack (schema M0b). Un pack può portarsi dietro
    provider di inferenza (anche con adapter-code). Ogni provider DEVE dichiarare
    `sovereignty` (seal + residenza + dpa + training): è dove escono i dati.
    `dpa_missing` = esiste un provider senza profilo DPA/sovranità completo
    → bloccante all'install + consenso owner obbligatorio."""
    provs = manifest.get("providers") or []
    out: list[dict[str, Any]] = []
    dpa_missing = False
    for p in provs:
        if not isinstance(p, dict):
            continue
        sov = p.get("sovereignty") or {}
        complete = bool(sov.get("seal")) and sov.get("dpa") is not None and bool(sov.get("residency"))
        if not complete:
            dpa_missing = True
        out.append({
            "name": p.get("name"),
            "sdk": p.get("sdk"),
            "base_url": p.get("base_url"),
            # adapter-code = codice provider di terzi nel percorso di inferenza →
            # review dinamica rigorosa (M4). null/assente = usa un sdk esistente.
            "adapter_code": bool(p.get("adapter")),
            "sovereignty": {
                "seal": sov.get("seal"),
                "residency": sov.get("residency"),
                "dpa": bool(sov.get("dpa")),
                "training": sov.get("training"),
            },
        })
    return {"providers": out, "dpa_missing": dpa_missing}


def _pack_license_info(umbrella: str, plugin_children: list) -> dict[str, Any]:
    """Licenza effettiva del pack. Umbrella = licenza del pack; ogni skill/plugin
    può override. Effettiva(skill) = skill.license or plugin.license or umbrella.
    `license_missing` = esiste una skill/plugin senza alcuna licenza nella catena
    (→ non installabile: contenuto a licenza ignota)."""
    umbrella = (umbrella or "").strip()
    effective: set[str] = set()
    missing = False
    for pl in plugin_children or []:
        pl_lic = str(pl.get("license") or "").strip()
        skills = pl.get("skills") or []
        if not skills:
            eff = pl_lic or umbrella
            if eff:
                effective.add(eff)
            else:
                missing = True
            continue
        for sk in skills:
            eff = str(sk.get("license") or "").strip() or pl_lic or umbrella
            if eff:
                effective.add(eff)
            else:
                missing = True
    return {"license": umbrella, "licenses": sorted(effective),
            "license_missing": missing}


def _pack_needs_setup(plugin_children: list) -> bool:
    """True se il pack ha qualcosa da PROVISIONARE (roba che il sysadmin deve
    rendere effettiva sul server MCP): server MCP da montare, collection RAG da
    ingerire, o datastore dichiarati. Un pack di sole skill/agent non richiede setup."""
    for c in plugin_children or []:
        if not isinstance(c, dict):
            continue
        if c.get("mcp_servers") or c.get("rag_collections") or c.get("datastores"):
            return True
    return False


def _setup_marker_path(name: str):
    return pack_import.PACKS_META_DIR / name / ".setup_pending"


def set_setup_pending(name: str, pending: bool) -> None:
    """Marca/smarca il setup del pack come pendente (marker file nel meta)."""
    p = _setup_marker_path(name)
    try:
        if pending:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
        elif p.exists():
            p.unlink()
    except OSError:
        pass


def _setup_pending(name: str, plugin_children: list) -> bool:
    """Setup pendente = il pack ha needs di setup E il marker è presente (setup
    non ancora completato). Marker assente = setup fatto (o non necessario)."""
    return _pack_needs_setup(plugin_children) and _setup_marker_path(name).is_file()


def _list_packs() -> list[dict[str, Any]]:
    plugin_items = {p["name"]: p for p in plugins_api.list_plugins()}
    installed_plugins = set(plugin_items)
    out: list[dict[str, Any]] = []
    meta_root = pack_import.PACKS_META_DIR
    if not meta_root.is_dir():
        meta_root.mkdir(parents=True, exist_ok=True)
    referenced: set[str] = set()
    for child in sorted(meta_root.iterdir()):
        manifest_path = child / "pack.yaml"
        if not child.is_dir() or not manifest_path.is_file():
            continue
        manifest = _load_pack_manifest(manifest_path)
        name = child.name
        agent_names = [str(a) for a in (manifest.get("agents") or [])]
        plugin_names = [str(p) for p in (manifest.get("plugins") or [])]
        referenced.update(plugin_names)
        agents = [_agent_entry(a, installed_plugins) for a in agent_names]
        plugin_children = [
            plugin_items.get(p, {"name": p, "missing": True}) for p in plugin_names
        ]
        lic = _pack_license_info(manifest.get("license") or "", plugin_children)
        prov = _pack_provider_info(manifest)
        installed_ver = str(manifest.get("version") or "").strip()
        out.append({
            "name": name,
            "description": str(manifest.get("description") or "").strip(),
            "version": installed_ver,
            # first-party con upstream → la UI mostra il tasto "Check update".
            # has_upstream via _pack_upstream: legge il manifest installato E fa
            # fallback al catalogo bundled (robusto se il manifest installato è
            # stato registrato senza il campo upstream).
            "first_party": bool(manifest.get("first_party")),
            "has_upstream": bool(_pack_upstream(name)),
            "source": str(manifest.get("source") or "").strip(),
            "agents": agents,
            "plugins": plugin_children,
            # Setup: il pack ha roba da provisionare (MCP/RAG/datastore) e il
            # setup non è ancora stato eseguito (marker) → la UI mostra "Finish setup".
            "needs_setup": _pack_needs_setup(plugin_children),
            "setup_pending": _setup_pending(name, plugin_children),
            "virtual": False,
            # first-party (base-pack e riservati) → non rimovibile
            "deletable": name not in pack_import.RESERVED_PACK_NAMES,
            "license": lic["license"],
            "licenses": lic["licenses"],
            "license_missing": lic["license_missing"],
            "providers": prov["providers"],
            "dpa_missing": prov["dpa_missing"],
            "third_party": bool(manifest.get("third_party")),
            "counts": {
                "agents": len(agents),
                "plugins": len(plugin_children),
            },
        })
    # Niente plugin sciolti (spec v0.3 §4b.3): ogni plugin senza pack è esposto
    # come pack VIRTUALE omonimo — il tree della webui mostra solo pack.
    already = {p["name"] for p in out}
    for pname, item in plugin_items.items():
        if pname in referenced or pname in already:
            continue
        lic = _pack_license_info(item.get("license") or "", [item])
        out.append({
            "name": pname,
            "description": item.get("description") or "",
            "version": item.get("version") or "",
            "source": item.get("source") or "",
            "agents": [],
            "plugins": [item],
            "needs_setup": _pack_needs_setup([item]),
            "setup_pending": _setup_pending(pname, [item]),
            "virtual": True,
            "deletable": bool(item.get("deletable", True))
            and pname not in pack_import.RESERVED_PACK_NAMES,
            "license": lic["license"],
            "licenses": lic["licenses"],
            "license_missing": lic["license_missing"],
            "license_note": item.get("license_note") or "",
            "providers": [],
            "dpa_missing": False,
            "third_party": bool(item.get("third_party")),
            "counts": {"agents": 0, "plugins": 1},
        })
    out.sort(key=lambda x: (x["name"] != "base-pack", x["name"]))
    return out


class PackImportUrl(BaseModel):
    url: str


@router.get("/clodia/packs")
async def list_packs() -> list[dict[str, Any]]:
    return _list_packs()


@router.get("/clodia/packs/{name}")
async def get_pack(name: str):
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    for pack in _list_packs():
        if pack["name"] == name:
            return pack
    return JSONResponse(status_code=404, content={"error": "pack non trovato"})


def _has_pack_ops_declarations(result: dict) -> bool:
    """True se l'import ha installato plugin con requires:/datastores: (pack ops)."""
    if result.get("kind") == "packs":
        return any(_has_pack_ops_declarations(r) for r in result.get("packs", []))
    return any(p.get("datastores") or p.get("requires")
               for p in result.get("plugins", []) if isinstance(p, dict))


def _maybe_trigger_pack_ops(result: dict) -> None:
    """Post-import: consegna la riconciliazione all'agente pack_ops (fire-and-forget).

    Solo se QUESTO import ha introdotto dichiarazioni — un import di sole
    skill non deve costare un run dell'agente sysadmin."""
    if not _has_pack_ops_declarations(result):
        return
    # Setup pendente per il/i pack importati (roba da provisionare) → "Finish setup".
    def _names(r: dict) -> list[str]:
        if r.get("kind") == "packs":
            out: list[str] = []
            for sub in r.get("packs", []):
                out += _names(sub)
            return out
        nm = r.get("pack") or r.get("name")
        return [nm] if nm else []
    for nm in _names(result):
        set_setup_pending(nm, True)
    import asyncio

    from . import pack_ops
    asyncio.create_task(pack_ops.trigger_reconcile("post-import"))
    result["pack_ops"] = {"scheduled": True}


@router.post("/clodia/packs/import")
async def import_pack_zip(request: Request, file: UploadFile = File(...)):
    """Import unificato da .zip: pack (agents+plugins) o plugin sciolto."""
    gateway_pdp.require_authz(request, "packs.import_url")  # admin-only (PDP gateway)
    from .skill_import import SkillImportError
    data = await file.read()
    try:
        result = pack_import.import_pack_zip(data, source=file.filename or "zip-upload")
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    plugins_api.invalidate_plugins()
    _maybe_trigger_pack_ops(result)
    return result


@router.post("/clodia/packs/import-url")
async def import_pack_url(payload: PackImportUrl, request: Request):
    """Import unificato da URL (git repo o .zip remoto)."""
    gateway_pdp.require_authz(request, "packs.import_url")  # admin-only (PDP gateway)
    from .skill_import import SkillImportError
    try:
        result = pack_import.import_pack_url(payload.url)
    except SkillImportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"import fallito: {str(e)[:160]}"})
    plugins_api.invalidate_plugins()
    _maybe_trigger_pack_ops(result)
    return result


@router.post("/clodia/packs/{name}/check-update")
async def check_pack_update(name: str, request: Request):
    """Controlla su GitHub (repo upstream del pack) se esiste una versione più
    recente di quella installata. Ritorna {installed, remote, update_available}."""
    gateway_pdp.require_authz(request, "packs.import_url")  # admin-only
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    up = _pack_upstream(name)
    if not up:
        return JSONResponse(status_code=400, content={
            "error": f"'{name}' non dichiara un upstream: check update non disponibile"})
    meta = pack_import.PACKS_META_DIR / name / "pack.yaml"
    installed = ""
    if meta.is_file():
        try:
            installed = str((yaml.safe_load(meta.read_text(encoding="utf-8")) or {}).get("version") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    try:
        remote = _fetch_remote_pack_version(up)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": f"check fallito: {str(e)[:160]}"})
    upd = bool(remote and _version_tuple(remote) > _version_tuple(installed or "0"))
    return {"name": name, "installed": installed, "remote": remote, "update_available": upd}


def _download_upstream_tarball(up: dict, tmp: Path) -> Path:
    import io
    import tarfile
    import httpx
    url = f"https://api.github.com/repos/{up['repo']}/tarball/{up['ref']}"
    headers = {}
    tok = _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = httpx.get(url, headers=headers, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        tf.extractall(tmp)  # il tar GitHub ha un unico top-dir <repo>-<sha>/
    tops = [c for c in tmp.iterdir() if c.is_dir()]
    if not tops:
        raise PackImportError("tarball vuoto")
    root = tops[0] / up["path"] if up["path"] else tops[0]
    if not (root / "pack.yaml").is_file():
        raise PackImportError(f"path '{up['path']}' senza pack.yaml nel repo")
    return root


@router.post("/clodia/packs/{name}/update")
async def update_pack(name: str, request: Request):
    """Aggiorna un pack first-party dal suo repo GitHub (upstream): scarica,
    SOSTITUISCE seed/skill/mcp (force), aggiorna il manifest e RIAVVIA tutti gli
    agenti (drop_all: le sessioni ripartono coi seed nuovi al prossimo messaggio)."""
    gateway_pdp.require_authz(request, "packs.import_url")  # admin-only (PDP gateway)
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    up = _pack_upstream(name)
    if not up:
        return JSONResponse(status_code=400, content={
            "error": f"'{name}' non dichiara un upstream: update non disponibile"})
    import tempfile
    from ..api.pack_import import PackImportError as _PIE
    try:
        with tempfile.TemporaryDirectory() as td:
            root = _download_upstream_tarball(up, Path(td))
            result = pack_import.install_pack_from_root(
                root, source=f"github:{up['repo']}", allow_reserved=True, force=True)
    except _PIE as e:
        return JSONResponse(status_code=400, content={"error": f"update: {str(e)[:160]}"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"update fallito: {str(e)[:160]}"})
    plugins_api.invalidate_plugins()
    try:
        registry.load()
    except Exception:  # noqa: BLE001
        pass
    # Setup pendente dopo un update: se il pack ha roba da provisionare (MCP/RAG/
    # datastore) va rifatto il setup → marker (la UI mostra "Finish setup").
    set_setup_pending(name, True)
    # Restart di tutti gli agenti: le sessioni vive ripartono coi seed aggiornati.
    stopped = []
    try:
        from ..sdk_runtime.session import manager
        stopped = await manager.drop_all()
    except Exception as e:  # noqa: BLE001
        LOG.warning("drop_all dopo update fallito: %s", e)
    new_ver = ""
    meta = pack_import.PACKS_META_DIR / name / "pack.yaml"
    if meta.is_file():
        try:
            new_ver = str((yaml.safe_load(meta.read_text(encoding="utf-8")) or {}).get("version") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    return {"updated": name, "version": new_ver, "agents_restarted": len(stopped),
            **(result or {})}


@router.post("/clodia/packs/{name}/setup-done")
async def mark_pack_setup_done(name: str, request: Request):
    """Marca il setup del pack come COMPLETATO (smarca il marker `setup_pending`).
    Lo chiama il sysadmin/steward alla fine del task di setup (tool gateway
    `packs.setup_done`), o l'admin manualmente. Admin-only (PDP gateway)."""
    gateway_pdp.require_authz(request, "packs.import_url")  # admin-only
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    set_setup_pending(name, False)
    return {"name": name, "setup_pending": False}


@router.delete("/clodia/packs/{name}")
async def delete_pack(name: str, request: Request):
    """Rimuove un pack: i suoi plugin, i suoi agenti (non nativi) e il manifest."""
    gateway_pdp.require_authz(request, "packs.remove")  # admin-only (PDP gateway)
    if not catalog._NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": "nome non valido"})
    # base-pack (e gli altri riservati) è first-party e NON è rimovibile — guardia
    # esplicita a monte, indipendente dal fatto che sia materializzato in DATA/packs.
    if name in pack_import.RESERVED_PACK_NAMES:
        return JSONResponse(
            status_code=403,
            content={"error": f"'{name}' è un pack first-party, non rimovibile"})
    try:
        result = pack_import.remove_pack(name)
    except KeyError:
        # pack virtuale (plugin senza manifest): delega alla rimozione plugin
        from .plugin_import import RESERVED_PLUGIN_NAMES, remove_plugin
        if name in RESERVED_PLUGIN_NAMES:
            return JSONResponse(status_code=403,
                                content={"error": f"'{name}' è nativo, non rimovibile"})
        removed = remove_plugin(name)
        if not removed:
            return JSONResponse(status_code=404, content={"error": "pack non trovato"})
        result = {"deleted": name, "plugins": [name], "agents": []}
    plugins_api.invalidate_plugins()
    return result
