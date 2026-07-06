"""
Endpoint per la lista dei topic e per il caricamento del summary.md.

Topic = sottocartella di topics/personal/ o topics/confidential/ che è un
repo git indipendente. La lista esclude le cartelle .archived/.

Implementa la card "FUN: Topics in sidebar" (Clodia Agency).
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
import mimetypes

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from ..config import workspace_path
from . import admin, topics_client
from . import access_log
from .agents import _principal_from_request

router = APIRouter()


TOPICS_ROOT = workspace_path("topics")
INDEX_DIRNAME = ".index"
CLASSIFICATIONS = ("personal", "confidential")
# Incrementare ogni volta che cambia lo schema del file indice .yaml:
# la mismatch invalida la cache e forza il rebuild anche senza modifiche al topic.
INDEX_SCHEMA_VERSION = 2
_ACTION_SECTION_TITLES = {
    "action point",
    "action points",
    "azioni",
    "next actions",
    "next steps",
    "prossime azioni",
    "prossimi passi",
    "todo",
    "to do",
}


def _git_last_commit_iso(topic_dir: Path) -> Optional[str]:
    """ISO 8601 timestamp dell'ultimo commit del repo, o None se non c'è git."""
    info = _git_last_commit_info(topic_dir)
    return info["date"] if info else None


def _git_last_commit_info(topic_dir: Path) -> Optional[dict[str, str]]:
    """Metadata essenziale dell'ultimo commit del repo topic."""
    if not (topic_dir / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(topic_dir), "log", "-1", "--format=%H%x00%cI%x00%s"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode != 0:
            return None
        raw = out.stdout.strip()
        if not raw:
            return None
        parts = raw.split("\x00", 2)
        if len(parts) != 3:
            return None
        return {"hash": parts[0], "date": parts[1], "subject": parts[2]}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _read_title(topic_dir: Path) -> str:
    """Title da meta.yaml, fallback al nome cartella."""
    meta = topic_dir / "meta.yaml"
    if meta.is_file():
        try:
            with open(meta, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            t = data.get("title")
            if isinstance(t, str) and t.strip():
                return t.strip()
        except (yaml.YAMLError, OSError):
            pass
    return topic_dir.name


#: Agent contact point di default quando il topic non lo dichiara in meta.yaml.
#: Per ora tutti i topic puntano a Clodia; in futuro ogni topic potrà indicare
#: nel proprio meta.yaml `contact_agent: <nome>` un agent specializzato.
DEFAULT_CONTACT_AGENT = "clodia"


def _read_contact_agent(topic_dir: Path) -> str:
    """Agent contact point da meta.yaml (`contact_agent`), fallback default.

    È l'agent con cui si apre la chat dedicata dalla card del topic in webui.
    """
    meta = topic_dir / "meta.yaml"
    if meta.is_file():
        try:
            with open(meta, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            a = data.get("contact_agent")
            if isinstance(a, str) and a.strip():
                return a.strip()
        except (yaml.YAMLError, OSError):
            pass
    return DEFAULT_CONTACT_AGENT


#: Stato del topic. La webui filtra di default gli `archived` (toggle per
#: mostrarli). `active|await|idle` sono tutti "non archiviati" e mostrati.
TOPIC_STATES = ("active", "await", "idle", "archived")
DEFAULT_STATUS = "active"
#: Mappa i valori legacy italiani al nuovo vocabolario.
_LEGACY_STATUS = {"attivo": "active", "in_attesa": "await", "completato": "idle"}


def _read_status(topic_dir: Path) -> str:
    """Stato del topic da meta.yaml (`status`), normalizzato al vocabolario
    active|await|idle|archived. Default `active`. Tollera i valori legacy IT."""
    meta = topic_dir / "meta.yaml"
    if meta.is_file():
        try:
            with open(meta, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            s = data.get("status")
            if isinstance(s, str) and s.strip():
                s = s.strip().lower()
                s = _LEGACY_STATUS.get(s, s)
                if s in TOPIC_STATES:
                    return s
        except (yaml.YAMLError, OSError):
            pass
    return DEFAULT_STATUS


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _parse_commit_dt(iso: Optional[str]) -> datetime:
    """Parsa una data commit ISO 8601 (con offset) a datetime aware, per
    ordinare per istante reale a prescindere dal fuso. Fallback all'epoch."""
    if not iso:
        return _EPOCH
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _EPOCH


def _summary_path(topic_dir: Path) -> Path:
    return topic_dir / "summary.md"


def _read_summary(topic_dir: Path) -> str:
    path = _summary_path(topic_dir)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _clean_summary_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^#{1,6}\s+", "", line)
    line = re.sub(r"^[-*+]\s+", "", line)
    line = re.sub(r"^\d+[.)]\s+", "", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def _trim_chars(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    clipped = text[: max(0, limit - 1)].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip()
    return f"{clipped}…"


def _extract_tldr(summary: str, limit: int = 400) -> str:
    """Sintesi fino a `limit` caratteri dall'inizio del summary.

    Non si limita alla prima riga: usa il titolo come lead e poi accumula la
    prosa/gli elenchi iniziali, saltando la nota di lettura (blockquote) e le
    etichette di sezione, fino a riempire il budget. Si ferma alla sezione
    degli action point così i todo non vengono duplicati nel TLDR (vanno in
    action_points). Se il summary è corto, il TLDR resta corto."""
    parts: list[str] = []
    total = 0
    for line in summary.splitlines():
        raw = line.strip()
        if not raw:
            continue
        heading = _heading_key(line)
        if heading is not None:
            # match per prefisso: cattura anche "prossimi passi (todo aperti)"
            if any(heading.startswith(a) for a in _ACTION_SECTION_TITLES):
                break
            if not parts:  # la prima riga di contenuto (il titolo) è il lead
                cleaned = _clean_summary_line(line)
                if cleaned:
                    parts.append(cleaned)
                    total += len(cleaned) + 1
            continue  # altre etichette di sezione: saltate (teniamo la prosa)
        if raw.startswith(">"):
            continue  # nota di lettura / blockquote
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line):
            continue  # elenco/bullet: i punti operativi vanno in action_points
        cleaned = _clean_summary_line(line)
        if cleaned:
            parts.append(cleaned)
            total += len(cleaned) + 1
        if total >= limit:
            break
    return _trim_chars(" ".join(parts), limit)


def _heading_key(line: str) -> str | None:
    match = re.match(r"^#{1,6}\s+(.+?)\s*$", line.strip())
    if not match:
        return None
    return match.group(1).strip().rstrip(":").lower()


def _bullet_text(line: str) -> str | None:
    cleaned = _clean_summary_line(line)
    if not cleaned:
        return None
    if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line):
        return cleaned
    return None


def _extract_action_points(summary: str) -> list[str]:
    """Estrae fino a tre azioni principali dal summary.

    Prima cerca bullet sotto una sezione nota ("Prossimi passi", "Action
    points", "TODO"). Se non ne trova, usa le prime bullet significative del
    documento come fallback.
    """
    section_bullets: list[str] = []
    fallback_bullets: list[str] = []
    in_action_section = False

    for line in summary.splitlines():
        heading = _heading_key(line)
        if heading is not None:
            in_action_section = heading in _ACTION_SECTION_TITLES
            continue

        bullet = _bullet_text(line)
        if not bullet:
            continue
        trimmed = _trim_chars(bullet, 120)
        if in_action_section and trimmed not in section_bullets:
            section_bullets.append(trimmed)
        if trimmed not in fallback_bullets:
            fallback_bullets.append(trimmed)

    return (section_bullets or fallback_bullets)[:3]


def _extract_recent_artifacts(topic_dir: Path, max_count: int = 3) -> list[dict]:
    """Ultimi `max_count` artefatti modificati nella cartella `files/`.

    Scansione ricorsiva di `files/`; ordina per mtime decrescente.
    Esclude dotfile. Ritorna lista di dict {name, path, mtime_iso}.
    """
    files_dir = topic_dir / "files"
    if not files_dir.is_dir():
        return []

    entries: list[tuple[float, Path]] = []
    try:
        for p in files_dir.rglob("*"):
            if p.name.startswith("."):
                continue
            if not p.is_file():
                continue
            try:
                entries.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except OSError:
        return []

    entries.sort(key=lambda t: t[0], reverse=True)
    result = []
    for mtime, p in entries[:max_count]:
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        rel = p.relative_to(topic_dir)
        result.append(
            {
                "name": p.name,
                "path": rel.as_posix(),
                "mtime_iso": dt.isoformat(),
            }
        )
    return result


def _index_filename(classification: str, name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{classification}__{name}")
    return f"{safe}.yaml"


def _topic_index_path(classification: str, name: str) -> Path:
    return TOPICS_ROOT / INDEX_DIRNAME / _index_filename(classification, name)


def _index_is_current(
    index_path: Path,
    summary_path: Path,
    meta_path: Path,
    commit: Optional[dict[str, str]],
) -> bool:
    if not index_path.is_file():
        return False
    try:
        data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    last_commit = data.get("last_commit") if isinstance(data, dict) else None
    indexed_hash = last_commit.get("hash") if isinstance(last_commit, dict) else None
    current_hash = commit.get("hash") if commit else None
    if indexed_hash != current_hash:
        return False
    if not summary_path.is_file():
        summary_current = data.get("summary_exists") is False
    else:
        try:
            summary_current = data.get("summary_mtime_ns") == summary_path.stat().st_mtime_ns
        except OSError:
            summary_current = False
    if not summary_current:
        return False

    if data.get("schema_version") != INDEX_SCHEMA_VERSION:
        return False
    if not meta_path.is_file():
        return data.get("meta_mtime_ns") is None
    try:
        return data.get("meta_mtime_ns") == meta_path.stat().st_mtime_ns
    except OSError:
        return False


def rebuild_topic_index(classification: str, name: str) -> dict:
    """Rigenera il file indice di un topic e ritorna il payload.

    Questo helper è intenzionalmente deterministico: non chiama modelli LLM.
    Va invocato dopo ogni update `.note` e viene usato da `GET /topics` come
    riparazione lazy quando l'indice manca o non è aggiornato.
    """
    if classification not in CLASSIFICATIONS:
        raise HTTPException(404, f"classification '{classification}' non valida")
    base = (TOPICS_ROOT / classification).resolve()
    topic_dir = (base / name).resolve()
    try:
        topic_dir.relative_to(base)
    except ValueError:
        raise HTTPException(400, "path traversal rilevato")
    if not topic_dir.is_dir():
        raise HTTPException(404, f"topic '{classification}/{name}' non trovato")

    summary = _read_summary(topic_dir)
    summary_path = _summary_path(topic_dir)
    meta_path = topic_dir / "meta.yaml"
    commit = _git_last_commit_info(topic_dir)
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "classification": classification,
        "name": name,
        "title": _read_title(topic_dir),
        "contact_agent": _read_contact_agent(topic_dir),
        "status": _read_status(topic_dir),
        "last_commit": commit,
        "summary_exists": summary_path.is_file(),
        "summary_mtime_ns": summary_path.stat().st_mtime_ns if summary_path.is_file() else None,
        "meta_mtime_ns": meta_path.stat().st_mtime_ns if meta_path.is_file() else None,
        "summary_path": f"{classification}/{name}/summary.md",
        "summary_url": f"/topics/{classification}/{name}/summary",
        "tldr": _extract_tldr(summary),
        "action_points": _extract_action_points(summary),
        "recent_artifacts": _extract_recent_artifacts(topic_dir),
    }

    index_path = _topic_index_path(classification, name)
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except OSError:
        # La lista topic deve restare disponibile anche se l'indice non è
        # scrivibile (es. filesystem read-only in diagnostica).
        pass
    return payload


def _load_or_rebuild_topic_index(classification: str, topic_dir: Path) -> dict:
    name = topic_dir.name
    summary_path = _summary_path(topic_dir)
    meta_path = topic_dir / "meta.yaml"
    commit = _git_last_commit_info(topic_dir)
    index_path = _topic_index_path(classification, name)
    if _index_is_current(index_path, summary_path, meta_path, commit):
        try:
            data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data
        except (OSError, yaml.YAMLError):
            pass
    return rebuild_topic_index(classification, name)


def rebuild_all_topic_indexes() -> dict:
    """Rigenera tutti gli indici dei topic attivi.

    Esclude cartelle dot-prefixed come `.archived` e `.index`. Utile come
    azione post-deploy o manutenzione, mentre `GET /topics` resta self-healing
    per il traffico normale.
    """
    rebuilt: list[dict] = []
    errors: list[dict] = []

    for classification in CLASSIFICATIONS:
        base = TOPICS_ROOT / classification
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                rebuilt.append(rebuild_topic_index(classification, entry.name))
            except HTTPException as exc:
                errors.append({
                    "classification": classification,
                    "name": entry.name,
                    "status": exc.status_code,
                    "detail": exc.detail,
                })
            except Exception as exc:  # pragma: no cover - defensive boundary
                errors.append({
                    "classification": classification,
                    "name": entry.name,
                    "status": 500,
                    "detail": str(exc),
                })

    return {
        "rebuilt": len(rebuilt),
        "errors": errors,
        "topics": rebuilt,
    }


def _scan(classification: str) -> list[dict]:
    """Scansiona topics/<classification>/ (esclude .archived/) e ritorna lista
    di topic indicizzati."""
    base = TOPICS_ROOT / classification
    if not base.is_dir():
        return []
    items: list[dict] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):  # esclude .archived e simili
            continue
        data = _load_or_rebuild_topic_index(classification, entry)
        last_commit = data.get("last_commit")
        items.append({
            "name": entry.name,
            "classification": classification,
            "title": data.get("title") or _read_title(entry),
            "contact_agent": data.get("contact_agent") or _read_contact_agent(entry),
            "status": data.get("status") or _read_status(entry),
            "last_commit": last_commit.get("date") if isinstance(last_commit, dict) else None,
            "last_commit_hash": last_commit.get("hash") if isinstance(last_commit, dict) else None,
            "last_commit_subject": last_commit.get("subject") if isinstance(last_commit, dict) else None,
            "summary_url": data.get("summary_url") or f"/topics/{classification}/{entry.name}/summary",
            "tldr": data.get("tldr") or "",
            "action_points": data.get("action_points") or [],
            "recent_artifacts": data.get("recent_artifacts") or [],
        })
    return items


@router.post("/api/topics/{tier}/{name}/archive")
def archive_topic(tier: str, name: str):
    """Archivia un topic (status=archived) via il gateway."""
    try:
        return topics_client.archive_topic(tier, name)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, str(e))


@router.post("/api/topics/{tier}/{name}/status")
async def set_topic_status(tier: str, name: str, request: Request):
    """Imposta lo status del topic (await|active|archived|urgent) via il gateway."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        return topics_client.set_status(tier, name, (body or {}).get("status", ""))
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, str(e))


@router.get("/api/topics/catalog")
async def topics_catalog(request: Request) -> list[dict]:
    """Catalogo COMPLETO dei topic (tier/name/title/kind) per il picker di export.
    Solo admin: bypassa l'ACL dell'index perché serve a chi esporta lo stato."""
    if not admin.is_admin(_principal_from_request(request)):
        raise HTTPException(403, "solo un admin può vedere il catalogo completo")
    try:
        rows = topics_client.list_topics()
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"gateway topics non disponibile: {str(e)[:160]}")
    return [{"tier": r.get("tier"), "name": r.get("name"),
             "title": r.get("title") or r.get("name"), "kind": r.get("kind")}
            for r in rows]


@router.get("/api/topics/export")
async def export_topics_bundle(request: Request, topics: str = ""):
    """Snapshot dei topic (tar.gz). `?topics=tier/name,...` per selezionarne
    alcuni, assente → tutti. Solo admin. Niente credenziali."""
    if not admin.is_admin(_principal_from_request(request)):
        raise HTTPException(403, "solo un admin può esportare lo stato dei topic")
    sel = [s.strip() for s in topics.split(",") if s.strip()] or None
    try:
        data = topics_client.export_bundle(sel)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"export fallito: {str(e)[:160]}")
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="clodia-topics-snapshot.tgz"'})


@router.post("/api/topics/import")
async def import_topics_bundle(request: Request) -> dict:
    """Importa i topic da uno snapshot (merge non-distruttivo). Solo admin."""
    if not admin.is_admin(_principal_from_request(request)):
        raise HTTPException(403, "solo un admin può importare topic")
    body = await request.body()
    if not body:
        raise HTTPException(400, "bundle vuoto")
    try:
        return topics_client.import_bundle(body)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"import fallito: {str(e)[:200]}")


@router.get("/topics")
async def list_topics(request: Request) -> list[dict]:
    """Lista topic dal Topic System v2 (dietro il gateway). La pagina Topics non
    cambia: mappiamo le righe dei verbi v2 alla forma `Topic` attesa dal FE.

    I DM (kind="dm") sono privati: vengono mostrati SOLO ai loro due partecipanti
    (filtro sul principal connesso). I canali normali restano visibili a tutti
    (l'accesso ai contenuti è comunque gated da _require_member su open/messages).

    (Transizione: i vecchi git-topic non passano più da qui — vedi spec v2.)"""
    try:
        rows = topics_client.list_topics()
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"gateway topics non disponibile: {str(e)[:160]}")
    me = _principal_from_request(request)
    access = access_log.all_times()
    items = [{
        "name": r.get("name"),
        "tier": r.get("tier"),
        "tier_name": r.get("tier_name"),
        "title": r.get("title"),
        "tldr": r.get("tldr", ""),
        "action_points": r.get("action_points", []),
        "contact_agent": r.get("contact_agent", "clodia"),
        "kind": r.get("kind"),
        "owner": r.get("owner"),
        "participants": r.get("participants", []),
        "status": r.get("status", "active"),
        # scadenza più vicina fra i todo (action_points) con data → badge in card
        "next_deadline": r.get("next_deadline"),
        "storage": r.get("storage"),
        "summary_url": f"/topics/{r.get('tier')}/{r.get('name')}/summary",
        "recent_artifacts": r.get("recent_files", []),
        # v2 non usa git: il "last_commit" della card è l'ultimo aggiornamento.
        "last_commit": r.get("updated_at"),
        # ultimo accesso dalla UI (apertura/scrittura del canale): ordina la lista.
        "last_accessed": access.get(f"{r.get('tier')}/{r.get('name')}"),
    } for r in rows if _visible_to(me, r)]
    # Ordina dal più recentemente consultato al più vecchio: usa last_accessed se
    # presente, altrimenti l'ultimo aggiornamento del contenuto. I topic mai
    # aperti né aggiornati finiscono in fondo.
    items.sort(key=lambda x: (x.get("last_accessed") or x.get("last_commit") or ""),
               reverse=True)
    return items


def _visible_to(me: str | None, row: dict) -> bool:
    """Un topic compare nell'index/preview SOLO a owner e partecipanti. Un agente
    non autorizzato non lo vede affatto (nessuna preview, nessun titolo)."""
    if not me:
        return False
    return me == row.get("owner") or me in (row.get("participants") or [])


@router.post("/topics/index/rebuild")
async def rebuild_all_topic_indexes_endpoint() -> dict:
    """Rigenera esplicitamente `topics/.index/` per tutti i topic attivi."""
    return rebuild_all_topic_indexes()


@router.get("/topics/{tier}/{name}/summary", response_class=PlainTextResponse)
async def get_topic_summary(tier: str, name: str) -> str:
    """Contenuto raw del summary del topic v2 (via gateway). `tier` = P0..P3."""
    try:
        info = topics_client.open_topic(tier, name)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"gateway topics non disponibile: {str(e)[:160]}")
    if info is None:
        raise HTTPException(404, f"topic non trovato: {tier}/{name}")
    return info.get("summary", "")


@router.post("/topics/{classification}/{name}/index/rebuild")
async def rebuild_topic_index_endpoint(classification: str, name: str) -> dict:
    """Rigenera esplicitamente l'indice card del topic.

    Pensato per il workflow `.note`: dopo update + commit del summary, il
    caller può invocare questo endpoint per riallineare `topics/.index/`.
    `GET /topics` fa comunque self-healing lazy se l'indice è stale.
    """
    return rebuild_topic_index(classification, name)


@router.post("/topics/{classification}/{name}/refresh")
async def refresh_topic_endpoint(classification: str, name: str) -> dict:
    """Allinea la working copy del topic all'origin e rigenera l'indice.

    Trigger event-driven del protocollo topic-management: dopo che un agent
    (es. sul Mac) ha fatto push al bare centrale, chiama questo endpoint sul
    host per fare ff-pull nella working copy che la webui legge e
    riallineare la card. Sostituisce il vecchio cron di polling.

    Sicuro: se la working copy è sporca (write in corso) salta il pull e fa
    solo il reindex; il pull non-ff non viene forzato (ritorna pull=skipped).
    """
    if classification not in CLASSIFICATIONS:
        raise HTTPException(404, f"classification '{classification}' non valida")
    base = (TOPICS_ROOT / classification).resolve()
    topic_dir = (base / name).resolve()
    try:
        topic_dir.relative_to(base)
    except ValueError:
        raise HTTPException(400, "path traversal rilevato")
    if not (topic_dir / ".git").exists():
        raise HTTPException(404, f"topic '{classification}/{name}' non è un repo git")

    pull = "skipped"
    # ff-pull solo se working tree pulito e remote 'origin' configurato.
    # "Sporco" = modifiche a file TRACCIATI: gli untracked NON bloccano un
    # ff-pull (coerente con l'helper topic.sh is_dirty).
    dirty = subprocess.run(["git", "-C", str(topic_dir), "status", "--porcelain",
                            "--untracked-files=no"],
                           capture_output=True, text=True, timeout=15).stdout.strip()
    has_origin = subprocess.run(["git", "-C", str(topic_dir), "remote"],
                                capture_output=True, text=True, timeout=15).stdout
    if dirty:
        pull = "skipped-dirty"
    elif "origin" not in has_origin.split():
        pull = "skipped-no-origin"
    else:
        br = subprocess.run(["git", "-C", str(topic_dir), "symbolic-ref", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=15).stdout.strip() or "main"
        r = subprocess.run(["git", "-C", str(topic_dir), "pull", "--ff-only", "origin", br],
                           capture_output=True, text=True, timeout=60)
        pull = "ok" if r.returncode == 0 else f"failed: {(r.stderr or r.stdout).strip()[:200]}"

    index = rebuild_topic_index(classification, name)
    return {"pull": pull, "index": {"tldr": index.get("tldr"),
                                    "action_points": index.get("action_points"),
                                    "last_commit": index.get("last_commit")}}


# ----- Tree endpoint ---------------------------------------------------------

_MAX_TREE_DEPTH = 5


def _scan_dir(dir_path: Path, topic_root: Path, depth: int) -> list[dict]:
    """Scansione ricorsiva di una directory dentro un topic.

    Ritorna lista di nodi {type, name, path, children?}. Esclude dotfile e
    .git/. `path` è relativo a `topic_root` con separatore POSIX (così il
    frontend può usarlo direttamente in querystring).
    """
    if depth > _MAX_TREE_DEPTH:
        return []
    out: list[dict] = []
    try:
        entries = sorted(
            dir_path.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        rel = entry.relative_to(topic_root).as_posix()
        if entry.is_dir():
            out.append(
                {
                    "type": "directory",
                    "name": entry.name,
                    "path": rel,
                    "children": _scan_dir(entry, topic_root, depth + 1),
                }
            )
        elif entry.is_file():
            out.append(
                {
                    "type": "file",
                    "name": entry.name,
                    "path": rel,
                }
            )
    return out


@router.get("/topics/{classification}/{name}/tree")
async def get_topic_tree(classification: str, name: str) -> dict:
    """Struttura ad albero del topic: nodo root con due figli (summary + files/).

    Solo due figli per design (spec sidebar resize/topic-tree):
    - `summary` (file): summary.md alla radice del topic, se esiste.
    - `files` (directory): contenuto ricorsivo di `files/` (esclude dotfile,
      depth massima _MAX_TREE_DEPTH).

    Anti path-traversal: tutto risolto sotto TOPICS_ROOT/<classification>.
    """
    if classification not in CLASSIFICATIONS:
        raise HTTPException(404, f"classification '{classification}' non valida")
    base = (TOPICS_ROOT / classification).resolve()
    topic_root = (base / name).resolve()
    try:
        topic_root.relative_to(base)
    except ValueError:
        raise HTTPException(400, "path traversal rilevato")
    if not topic_root.is_dir():
        raise HTTPException(404, f"topic '{classification}/{name}' non trovato")

    children: list[dict] = []

    summary = topic_root / "summary.md"
    if summary.is_file():
        children.append(
            {
                "type": "file",
                "name": "summary.md",
                "path": "summary.md",
            }
        )

    files_dir = topic_root / "files"
    if files_dir.is_dir():
        children.append(
            {
                "type": "directory",
                "name": "files",
                "path": "files",
                "children": _scan_dir(files_dir, topic_root, depth=1),
            }
        )

    return {
        "name": name,
        "classification": classification,
        "title": _read_title(topic_root),
        "children": children,
    }


# ----- File endpoint (read-only, plaintext) ----------------------------------

# Estensioni testuali per cui ha senso fare PlainTextResponse inline.
_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".csv", ".tsv",
    ".log", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml",
    ".json", ".sh", ".py", ".js", ".ts", ".html", ".xml",
}

_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB hard cap per il viewer


@router.get("/topics/{classification}/{name}/file", response_class=PlainTextResponse)
async def get_topic_file(
    classification: str,
    name: str,
    path: str = Query(..., description="path relativo al topic root"),
) -> str:
    """Contenuto raw di un file dentro il topic. Read-only, plaintext.

    `path` è relativo al topic root (es. 'files/foo.md'). Anti path-traversal:
    il path risolto deve stare sotto TOPICS_ROOT/<classification>/<name>.
    Cap a _MAX_FILE_BYTES e whitelist di estensioni testuali.
    """
    if classification not in CLASSIFICATIONS:
        raise HTTPException(404, f"classification '{classification}' non valida")
    if not path or path.startswith("/"):
        raise HTTPException(400, "path deve essere relativo, non vuoto")

    base = (TOPICS_ROOT / classification).resolve()
    topic_root = (base / name).resolve()
    try:
        topic_root.relative_to(base)
    except ValueError:
        raise HTTPException(400, "path traversal rilevato")
    if not topic_root.is_dir():
        raise HTTPException(404, f"topic '{classification}/{name}' non trovato")

    target = (topic_root / path).resolve()
    try:
        target.relative_to(topic_root)
    except ValueError:
        raise HTTPException(400, "path traversal rilevato")
    if not target.is_file():
        raise HTTPException(404, f"file non trovato: {path}")

    if target.suffix.lower() not in _TEXT_EXTENSIONS:
        raise HTTPException(415, f"estensione '{target.suffix}' non supportata dal viewer")

    try:
        size = target.stat().st_size
    except OSError as e:
        raise HTTPException(500, f"errore stat: {e}")
    if size > _MAX_FILE_BYTES:
        raise HTTPException(413, f"file troppo grande ({size} > {_MAX_FILE_BYTES})")

    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "file non è UTF-8 valido")
    except OSError as e:
        raise HTTPException(500, f"errore lettura: {e}")


_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB hard cap per il download


def _download_scope(tier: str, name: str, path: str) -> str:
    return f"topic|{tier}|{name}|{path}"


@router.get("/topics/{tier}/{name}/download-url")
async def topic_download_url(
    request: Request,
    tier: str,
    name: str,
    path: str = Query(...),
):
    """URL firmato a scadenza per il download (per i link <a> del browser).
    Richiede login + membership del canale — è QUI che si paga l'ACL."""
    from urllib.parse import quote

    from . import download_sign
    from .channels import _require_member
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "topic non trovato")
    _require_member(request, topic.get("meta", {}))
    exp, sig = download_sign.make(_download_scope(tier, name, path))
    return {"url": (f"/topics/{quote(tier)}/{quote(name)}/download"
                    f"?path={quote(path)}&exp={exp}&sig={sig}"),
            "expires_in": max(0, exp - __import__("time").time().__int__())}


@router.get("/topics/{tier}/{name}/download")
async def download_topic_file(
    request: Request,
    tier: str,
    name: str,
    path: str = Query(..., description="path relativo al topic (es. files/report.pdf)"),
    exp: int | None = None,
    sig: str | None = None,
):
    """Download di un file dentro il topic v2 (via gateway). `tier` = P0..P3.

    AUTENTICATO (fix 7 lug 2026 — prima era aperto: chiunque col link
    scaricava, anche da tier confidenziali): serve una firma valida a
    scadenza (URL presigned da /download-url) OPPURE una sessione con
    membership del canale."""
    if not path or path.startswith("/"):
        raise HTTPException(400, "path deve essere relativo, non vuoto")
    from . import download_sign
    if not (exp and sig and download_sign.verify(_download_scope(tier, name, path), exp, sig)):
        from .channels import _require_member
        topic = topics_client.open_topic(tier, name)
        if not topic:
            raise HTTPException(404, "topic non trovato")
        _require_member(request, topic.get("meta", {}))
    try:
        data = topics_client.get_file(tier, name, path)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"gateway topics non disponibile: {str(e)[:160]}")
    if data is None:
        raise HTTPException(404, f"file non trovato: {tier}/{name}/{path}")
    fname = path.rsplit("/", 1)[-1]
    media_type = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    from fastapi.responses import Response
    return Response(
        content=data, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
