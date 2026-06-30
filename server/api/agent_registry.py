"""API REST della agent registry.

NOTA: non confondere con `api/agents.py`, che gestisce le chat multi-Clodia
(endpoint `/clodia/chats/*`). Questo modulo espone la registry degli agenti
specializzati definiti in `clodia-data/agents/` (endpoint `/api/agents/*`).

Modello inbox v3: un unico consumer (`/api/agents/consumer/*`) gestisce
tutte le inbox-lane. Niente più PM separato.

Storia del prefisso: fino al 30 mag 2026 le API erano esposte sotto `/agents`,
ma collidevano con la rotta SvelteKit `/agents` della GUI (F5 sulla pagina
serviva JSON invece dell'HTML perché il router FastAPI matchava prima del
catch-all SPA). Lo spostamento sotto `/api/agents` libera il path per la SPA.
"""
import asyncio
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CLR_LEGACY = {"P0": "SEAL-0", "P1": "SEAL-1", "P2": "SEAL-2", "P3": "SEAL-3"}
_CLR_VALID = ("SEAL-0", "SEAL-1", "SEAL-2", "SEAL-3", "SEAL-4")

def _norm_clearance(c: str | None) -> str:
    u = (c or "SEAL-0").strip().upper()
    return _CLR_LEGACY.get(u, u)


import yaml

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..agents import activity_log, pause as pause_mod, rank as rank_mod, registry
from ..agents.models import AgentSpec
from .providers import connected_provider_ids, candidate_providers, effective_provider, provider_seal
from .provider_store import ProviderStoreError
from . import admin, contacts, imagegen_client
from .agents import _principal_from_request

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _require_self_or_admin(request: Request, name: str) -> None:
    """I dettagli di un profilo agent sono visibili SOLO al diretto interessato
    (principal == name) o a un admin. Un umano non vede i dettagli altrui."""
    me = _principal_from_request(request)
    if admin.is_admin(me) or (me and me == name):
        return
    raise HTTPException(403, "puoi vedere i dettagli solo del tuo profilo")

# Stile sempre applicato alla PFP generata (richiesta di owner): qualunque sia
# l'input (prompt o immagine caricata), il risultato passa da gpt-image-2 con
# questo stile appeso.
_PFP_STYLE = "no fotorealistic, manga style, studio ghibli style"


def _connected_safe() -> set[str]:
    """Provider collegati, con degrado a vuoto se il gateway è irraggiungibile:
    la lista agenti non deve andare in 500 perché il vault è momentaneamente giù."""
    try:
        return connected_provider_ids()
    except ProviderStoreError:
        return set()


def _provider_fields(spec: AgentSpec, connected: set[str]) -> dict:
    """Provider risolto (esplicito o derivato dall'agent_sdk) + flag di
    connessione. Completa lo stack agent/model/provider nelle risposte API e
    permette alla webui di marcare 'disconnected' gli agent il cui provider
    non è collegato."""
    # I principal `human` non sono eseguiti: nessun provider/motore.
    if spec.type == "human":
        return {"provider": None, "providers": [], "provider_connected": True}
    cands = candidate_providers(getattr(spec, "providers", None),
                                getattr(spec, "provider", None), spec.agent_sdk)
    # provider EFFETTIVO con la policy SEAL-max fra gli attivi (connessi e non in pausa).
    pid = effective_provider(getattr(spec, "providers", None),
                             getattr(spec, "provider", None), spec.agent_sdk, connected)
    return {
        "provider": pid,
        # SEAL del provider a cui l'agent è ATTUALMENTE attribuito (per la card).
        "provider_seal": provider_seal(pid),
        # lista ordinata dei provider compatibili (per la UI).
        "providers": cands,
        # se ci sono candidati ma nessuno attivo → agent disattivato.
        "provider_connected": (pid is not None) if cands else True,
    }


def _identity_info(name: str) -> dict | None:
    """Identità PKI dell'agente per la scheda (fingerprint cert, validità,
    revoca). Mai chiavi: solo metadati pubblici del certificato."""
    try:
        from ..colony import pki
        cert_path = pki.agent_cert_path(name)
        if not cert_path.is_file():
            return None
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return {
            "cert_fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "not_before": cert.not_valid_before_utc.isoformat(),
            "not_after": cert.not_valid_after_utc.isoformat(),
            "revoked": pki.is_revoked(name),
        }
    except Exception:
        return None


def _success_stats(name: str) -> dict | None:
    """Contatori di esito dell'agente. Provenivano dal DB della colony
    (Selection Engine), rimossa il 20 giu 2026 → sempre None. Il campo resta
    nella risposta API per compatibilità col frontend."""
    return None


@router.get("")
async def list_agents() -> dict:
    paused = set(pause_mod.list_paused())
    connected = _connected_safe()
    agents = []
    for a in registry.list():
        d = a.model_dump()
        d["paused"] = a.name in paused
        d["identity"] = _identity_info(a.name)
        d["success_stats"] = _success_stats(a.name)
        d["rank_tier"] = rank_mod.rank_tier(a)
        d["rank_label"] = rank_mod.rank_label(a)
        d.update(_provider_fields(a, connected))
        agents.append(d)
    return {
        "agents": agents,
        "errors": registry.errors(),
        "base_dir": str(registry.base_dir),
    }


@router.get("/{name}", response_model=None)
async def get_agent(name: str, request: Request) -> dict:
    spec = registry.get_by_name(name)
    if spec is None:
        raise HTTPException(404, f"agent '{name}' non trovato")
    _require_self_or_admin(request, name)
    d = spec.model_dump()
    d["paused"] = pause_mod.is_paused(name)
    d["identity"] = _identity_info(name)
    d["success_stats"] = _success_stats(name)
    d["rank_tier"] = rank_mod.rank_tier(spec)
    d["rank_label"] = rank_mod.rank_label(spec)
    d["contact_channels"] = contacts.channels(spec)
    d.update(_provider_fields(spec, _connected_safe()))
    return d


@router.get("/{name}/pfp")
async def get_agent_pfp(name: str):
    """Ritorna l'immagine `pfp.png` dell'agent, se presente nella sua agent_dir.

    La WebUI la usa come avatar tonale al posto del fallback iniziale+colore.
    """
    spec = registry.get_by_name(name)
    if spec is None:
        raise HTTPException(404, f"agent '{name}' non trovato")
    pfp = Path(spec.agent_dir) / "pfp.png"
    if not pfp.is_file():
        raise HTTPException(404, "pfp non disponibile")
    return FileResponse(pfp, media_type="image/png")


class PfpGenerateBody(BaseModel):
    prompt: Optional[str] = None        # descrizione testuale dell'avatar
    image_b64: Optional[str] = None     # immagine caricata (data URL o base64) → restyle


@router.post("/{name}/pfp/generate")
async def generate_agent_pfp(name: str, body: PfpGenerateBody) -> dict:
    """Genera la PFP dell'agent via gpt-image-2 (sul gateway) e la salva in
    `agent_dir/pfp.png`. Accetta un prompt testuale OPPURE un'immagine caricata
    (che viene ri-renderizzata nello stile): in entrambi i casi lo stile manga/
    ghibli è applicato. La OpenAI key vive solo nel gateway (vault)."""
    spec = registry.get_by_name(name)
    if spec is None:
        raise HTTPException(404, f"agent '{name}' non trovato")
    prompt = (body.prompt or "").strip()
    if not prompt and not body.image_b64:
        raise HTTPException(400, "serve un prompt testuale o un'immagine")
    # Base del prompt: l'utente, o un default sensato sull'identità dell'agent.
    if prompt:
        base = prompt
    elif body.image_b64:
        base = f"ritratto avatar di {spec.display_name or name}, mantieni il soggetto"
    else:
        base = f"ritratto avatar di {spec.display_name or name}"
    styled = f"{base}, {_PFP_STYLE}"
    try:
        png = imagegen_client.generate(styled, image_b64=body.image_b64)
    except imagegen_client.ImageGenUnavailable as e:
        raise HTTPException(409, str(e))
    except imagegen_client.ImageGenError as e:
        raise HTTPException(502, f"generazione immagine fallita: {str(e)[:200]}")
    pfp = Path(spec.agent_dir) / "pfp.png"
    try:
        pfp.write_bytes(png)
    except OSError as e:
        raise HTTPException(500, f"salvataggio pfp fallito: {e}")
    return {"ok": True, "name": name, "bytes": len(png)}


@router.post("/reload")
async def reload_agents() -> dict:
    registry.load()
    return {
        "loaded": len(registry.list()),
        "errors": registry.errors(),
    }


# ── Activity log ─────────────────────────────────────────────────────────


@router.get("/activity/summary")
async def activity_summary() -> dict:
    """Per ogni agente: count run di oggi, status, ultimo evento."""
    return {"agents": activity_log.summary()}


@router.get("/{name}/activity")
async def agent_activity(name: str, request: Request, limit: int = 200, date: Optional[str] = None) -> dict:
    """Eventi cronologici di un agente per la data (default oggi)."""
    if registry.get_by_name(name) is None:
        raise HTTPException(404, f"agent '{name}' non trovato")
    _require_self_or_admin(request, name)
    return {
        "agent": name,
        "events": activity_log.tail(name, limit=limit, date=date),
    }


# ── Write: leggi/edita il system prompt, crea agente ────────────────────
# Necessari alla WebUI (sezione AGENTS azioni write). Scrivono nei file degli
# agenti sotto la datadir; `name` è sanitizzato contro path traversal.


@router.get("/{name}/system-prompt")
async def get_system_prompt(name: str, request: Request) -> dict:
    """Ritorna il BODY del system prompt (il GET /{name} espone solo il nome file)."""
    spec = registry.get_by_name(name)
    if spec is None:
        raise HTTPException(404, f"agent '{name}' non trovato")
    _require_self_or_admin(request, name)
    path = Path(spec.agent_dir) / spec.system_prompt
    if not path.is_file():
        raise HTTPException(404, f"file prompt '{spec.system_prompt}' mancante per '{name}'")
    return {"name": name, "filename": spec.system_prompt, "body": path.read_text()}


class AgentPatch(BaseModel):
    system_prompt: Optional[str] = None   # BODY del prompt (non il filename)
    agent_sdk: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    display_name: Optional[str] = None
    # meta + canali di contatto (admin). Stringa vuota "" = rimuovi il campo.
    avatar_color: Optional[str] = None
    clearance: Optional[str] = None
    email: Optional[str] = None
    telegram: Optional[str] = None          # opzionale
    mailbox_parent: Optional[str] = None    # super genitore per il subaddress (regular)


def _set_yaml_scalar(text: str, key: str, value: str) -> str:
    """Sostituisce/aggiunge un campo scalare top-level in YAML preservando
    commenti e formattazione del resto."""
    val = json.dumps(value)  # double-quoted, yaml-safe per scalari stringa
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if pat.search(text):
        return pat.sub(f"{key}: {val}", text, count=1)
    return text.rstrip("\n") + f"\n{key}: {val}\n"


def _remove_yaml_scalar(text: str, key: str) -> str:
    """Rimuove un campo scalare top-level (per azzerare un valore opzionale)."""
    return re.sub(rf"^{re.escape(key)}:.*$\n?", "", text, count=1, flags=re.MULTILINE)


@router.patch("/{name}", response_model=AgentSpec)
async def patch_agent(name: str, patch: AgentPatch, request: Request) -> AgentSpec:
    """Edita system prompt, meta, canali di contatto, model, sdk di un agent
    (anche super). SOLO admin."""
    if not admin.is_admin(_principal_from_request(request)):
        raise HTTPException(403, "solo un admin può modificare un agent")
    spec = registry.get_by_name(name)
    if spec is None:
        raise HTTPException(404, f"agent '{name}' non trovato")
    if patch.clearance is not None and patch.clearance and _norm_clearance(patch.clearance) not in _CLR_VALID:
        raise HTTPException(400, f"clearance invalida: {patch.clearance} (SEAL-0..4)")
    agent_dir = Path(spec.agent_dir)

    if patch.system_prompt is not None:
        (agent_dir / spec.system_prompt).write_text(patch.system_prompt)

    # campi scalari di agent.yaml: set se valorizzato, rimuovi se stringa vuota.
    _scalars = {
        "agent_sdk": patch.agent_sdk, "model": patch.model,
        "description": patch.description, "display_name": patch.display_name,
        "avatar_color": patch.avatar_color, "clearance": patch.clearance,
        "email": patch.email, "telegram": patch.telegram,
        "mailbox_parent": patch.mailbox_parent,
    }
    if any(v is not None for v in _scalars.values()):
        yaml_path = agent_dir / "agent.yaml"
        text = yaml_path.read_text()
        for key, value in _scalars.items():
            if value is None:
                continue
            if value == "":
                text = _remove_yaml_scalar(text, key)
            else:
                text = _set_yaml_scalar(text, key, value if key != "clearance" else value.upper())
        yaml_path.write_text(text)

    registry.load()
    updated = registry.get_by_name(name)
    if updated is None:
        raise HTTPException(500, f"dopo la modifica l'agent '{name}' non valida: {registry.errors().get(name)}")
    return updated


class AgentCreate(BaseModel):
    name: str
    agent_sdk: str = "claude"
    model: str = "claude-haiku-4-5-20251001"
    display_name: Optional[str] = None
    description: str = ""
    avatar_color: str = "#888888"
    # categoria KYA: gli agent user-defined nascono "normal". super = solo i
    # nativi clodia/ophelia (non ricreabili da qui).
    type: str = "normal"
    # costituzione di default per i nuovi agent (baseline lite). "none" = nessuna.
    constitution: Optional[str] = "platform-core"
    # ancestor (1-2) da cui ereditare le skill come attributi di specie.
    parents: list[str] = []
    # Admin Auth: per type=human, pubkey ed25519 (PEM) generata dal browser →
    # la CA emette il cert. Il PRIMO human creato diventa superadmin (claim
    # dell'istanza). Il server non vede mai la privkey.
    pubkey: Optional[str] = None
    # Clearance di privacy del principal umano (P0–P3): vede un topic sse
    # T.privacy <= clearance. La sceglie l'admin alla creazione (default P0).
    clearance: Optional[str] = None
    # Canali di contatto (umani: email + telegram; valorizzati anche dalla
    # cert-request approvata). Per i regular l'email si deriva (subaddress).
    email: Optional[str] = None
    telegram: Optional[str] = None


# Nomi riservati ai super-agent nativi (seed nel repo, non ricreabili via API).
_NATIVE_AGENTS = {"clodia", "ophelia"}


@router.post("", status_code=201, response_model=AgentSpec)
async def create_agent(body: AgentCreate) -> AgentSpec:
    """Crea un nuovo agente USER-DEFINED generando lo scaffold direttamente dallo
    schema (single source of truth — niente file-template da tenere allineato),
    poi reload. Solo clodia/ophelia sono nativi (seed nel repo)."""
    name = body.name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", name):
        raise HTTPException(400, "nome invalido (usa [a-z0-9_-], inizia con alfanumerico)")
    if name in _NATIVE_AGENTS:
        raise HTTPException(409, f"'{name}' è un super-agent nativo, non ricreabile da qui")
    target = registry.base_dir / name
    if target.exists():
        raise HTTPException(409, f"agent '{name}' esiste già")

    display = body.display_name or name.capitalize()
    created_at = datetime.now(timezone.utc).isoformat()  # anzianità (tie-break rango)

    if body.type == "human":
        # Principal UMANO (Admin Auth): è un'identità, NON un agente eseguito →
        # niente motore (model/agent_sdk), niente sandbox/system-prompt/memory.
        # Il PRIMO human reclama l'istanza come superadmin. Se arriva la pubkey
        # del browser la CA emette il cert (il server non vede mai la privkey).
        from . import admin as _admin
        # Il PRIMO human (claim) è superadmin; gli altri sono 'member' — utenti
        # umani che chattano con gli agent, NON amministratori. La clearance la
        # sceglie l'admin (default P0 = vede solo i topic pubblici).
        clearance = _norm_clearance(body.clearance)
        if clearance not in _CLR_VALID:
            raise HTTPException(400, f"clearance invalida: {clearance} (SEAL-0..4)")
        spec_yaml = {
            "name": name,
            "display_name": display,
            "description": body.description or f"Principal umano {name}",
            "type": "human",
            "role": "superadmin" if not _admin.is_initialized() else "member",
            "clearance": clearance,
            "avatar_color": body.avatar_color,
            "created_at": created_at,
        }
        # canali di contatto dell'umano (email/telegram), se forniti
        if body.email:
            spec_yaml["email"] = body.email.strip()
        if body.telegram:
            spec_yaml["telegram"] = body.telegram.strip()
        if body.pubkey:
            from ..colony import pki
            try:
                pki.issue_cert_for_pubkey(name, body.pubkey)
            except Exception as e:  # noqa: BLE001 — superficie esterna
                raise HTTPException(400, f"emissione certificato fallita: {str(e)[:160]}")
        try:
            target.mkdir(parents=True)
            (target / "agent.yaml").write_text(
                yaml.safe_dump(spec_yaml, sort_keys=False, allow_unicode=True))
        except Exception as e:
            shutil.rmtree(target, ignore_errors=True)
            raise HTTPException(500, f"creazione fallita: {e}")
    else:
        spec_yaml: dict = {
            "name": name,
            "display_name": display,
            "description": body.description or f"Agente {name}",
            "type": body.type,
            "agent_sdk": body.agent_sdk,
            "model": body.model,
            "avatar_color": body.avatar_color,
            "created_at": created_at,
        }
        if body.constitution and body.constitution.lower() != "none":
            spec_yaml["constitution"] = body.constitution
        if body.parents:
            spec_yaml["parents"] = [p.strip() for p in body.parents if p.strip()][:2]
        spec_yaml.update({
            "sandbox": {
                "allow_read": ["{scratch}/**"],
                "deny_read": ["secrets/**", "topics/**"],
                "allow_write": ["{scratch}/**"],
                "allow_shell_cmds": [],
                "deny_shell_patterns": ["rm -rf *", "sudo *"],
            },
            "capabilities": [],
            "rules": [],
            "tool_permissions": [],
            "memory": {"dir": "memory/"},
            "system_prompt": "system-prompt.md",
        })
        system_prompt = (
            f"# {display}\n\n{body.description or ''}\n\n"
            "(Definisci qui identità, compiti e modo di operare dell'agente. "
            "Le Leggi della Robotica arrivano dal layer costituzione: non vanno "
            "ripetute qui.)\n"
        )
        try:
            target.mkdir(parents=True)
            (target / "agent.yaml").write_text(
                yaml.safe_dump(spec_yaml, sort_keys=False, allow_unicode=True))
            (target / "system-prompt.md").write_text(system_prompt)
            (target / "memory").mkdir()
            (target / "memory" / "MEMORY.md").write_text("# Memory Index\n")
        except Exception as e:
            shutil.rmtree(target, ignore_errors=True)
            raise HTTPException(500, f"creazione fallita: {e}")

    registry.load()
    created = registry.get_by_name(name)
    if created is None:
        shutil.rmtree(target, ignore_errors=True)
        registry.load()
        raise HTTPException(500, f"agent creato ma non valido: {registry.errors().get(name)}")
    return created
