"""Runtime del canale (Fase 2) — i topic come canali Slack-like.

Un **post umano** (o un **@tag**) innesca UN solo risponditore: l'AI taggato se
presente, altrimenti il partecipante AI di **rango più alto** (rank.py), filtrato
per **clearance** (`T.privacy ≤ agente.clearance`). Il turno RIUSA il runtime
delle chat (ChatSession/CodexChatSession: spawn, provider, principal, log); la
risposta viene postata nel canale (`.messages/`). Niente catene AI→AI automatiche.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from ..agents import activity_log, rank as rank_mod, registry
from ..core.events import bus
from ..core.models import Event, MessageRequest
from ..sdk_runtime.session import manager, ProviderNotConnected
from . import topics_client
from . import access_log
from .agents import _principal_from_request

router = APIRouter()
LOG = logging.getLogger("agent-server.api.channels")


async def _typing(tier: str, name: str, agent: str, state: str) -> None:
    """Pubblica un evento di typing sul bus SSE (/clodia/events) così la UI
    mostra 'X sta scrivendo…'. state = start|stop. Best-effort."""
    try:
        await bus.publish(Event(
            type="channel_typing",
            payload={"tier": tier, "name": name, "agent": agent, "state": state},
            timestamp=datetime.now(timezone.utc),
        ))
    except Exception as e:  # noqa: BLE001
        LOG.debug("typing event non pubblicato: %s", e)

# I DM sono canali a 2 partecipanti (meta.kind="dm"): nome deterministico (i due
# nomi ordinati) così "owner↔clodia" e "clodia↔owner" sono lo STESSO canale.
# Tier P0: l'accesso è ristretto ai 2 membri dal gate _require_member, non dal
# tier; P0 garantisce che l'AeI coinvolto possa sempre rispondere (clearance≥P0).
_DM_TIER = "SEAL-0"


def _dm_name(a: str, b: str) -> str:
    x, y = sorted([a.strip().lower(), b.strip().lower()])
    return f"dm-{x}--{y}"

_CLEAR = {"SEAL-0": 0, "SEAL-1": 1, "SEAL-2": 2, "SEAL-3": 3, "SEAL-4": 4}
_LEGACY_TIER = {"P0": "SEAL-0", "P1": "SEAL-1", "P2": "SEAL-2", "P3": "SEAL-3"}


def _norm(level: str | None) -> str:
    u = (level or "SEAL-0").strip().upper()
    return _LEGACY_TIER.get(u, u)
_TAG_RE = re.compile(r"@([a-z0-9][a-z0-9_-]{0,30})")


def _effective_clearance(spec) -> str:
    """I super-agent (clodia/ophelia) sono full-power → clearance massima (P3).
    Gli altri usano la clearance dichiarata (default P0)."""
    if getattr(spec, "type", None) == "super":
        return "SEAL-4"
    return _norm(getattr(spec, "clearance", None))


def _can_access(clearance: str | None, tier: str | None) -> bool:
    """T.privacy <= clearance: l'agente vede il canale se la sua clearance ≥ tier."""
    return _CLEAR.get(_norm(clearance), 0) >= _CLEAR.get(_norm(tier), 0)


def _tagged(text: str) -> str | None:
    m = _TAG_RE.findall(text or "")
    return m[0] if m else None


def _channel_meta(body: dict, principal: str, name: str) -> dict:
    contact_agent = (body.get("contact_agent") or "clodia").strip().lower()
    meta = {
        "title": (body.get("title") or name),
        "type": body.get("type") or "progetto",
        "owner": principal,
        "participants": list(dict.fromkeys([principal, contact_agent])),
        "contact_agent": contact_agent,
    }
    # Storage backend dei FILE (scelto in UI): local (default) o drive.
    # Il gateway (service.new) lo materializza: drive → lega/crea la cartella.
    sc = body.get("storage_config")
    if isinstance(sc, dict) and sc.get("type") == "drive":
        meta["storage_config"] = {"type": "drive",
                                  "folder": (sc.get("folder") or "").strip() or None,
                                  "account": sc.get("account")}
    return meta


def _provider_seal_ok(spec, tier: str | None) -> bool:
    """True se il provider EFFETTIVO dell'agent ha SEAL ≥ tier del topic — cioè il
    motore che tratterà i dati è adeguato al tier. Provider non determinato → non ok
    (salvo tier SEAL-0)."""
    from ..sdk_runtime.session import agent_effective_provider
    from .providers import provider_seal
    ps = provider_seal(agent_effective_provider(spec.name))
    return _CLEAR.get(_norm(ps), 0) >= _CLEAR.get(_norm(tier), 0)


def _eligibility(spec, tier: str | None) -> dict:
    """Idoneità di un AeI al tier del topic, per la UI.
    - umani: sempre idonei (non trattano dati via provider).
    - normal: idoneo solo se clearance ≥ tier E provider.seal ≥ tier.
    - super: sempre idoneo (eccezione clodia), ma `warn` se il provider è sotto
      il tier → la UI lo mostra con ⚠️."""
    if not spec or spec.type not in ("super", "normal"):
        return {"eligible": True, "warn": False}
    clr_ok = _can_access(_effective_clearance(spec), tier)
    prov_ok = _provider_seal_ok(spec, tier)
    if spec.type == "super":
        return {"eligible": True, "warn": not prov_ok}
    return {"eligible": bool(clr_ok and prov_ok), "warn": False}


def _pick_responder(participants: list[str], tier: str, tagged: str | None):
    """AI partecipante taggato (se idoneo), altrimenti il più alto di rango fra gli
    AI idonei. Idoneità (30 giu 2026): clearance ≥ tier SEMPRE; inoltre, per gli
    agent NORMAL, anche provider.seal ≥ tier (enforcement duro). I super-agent
    (clodia) bypassano il vincolo provider → rispondono comunque, ma chi chiama
    riceve un warning se il provider è sotto il tier (vedi channel_post)."""
    specs = [registry.get_by_name(n) for n in participants]

    def eligible(s) -> bool:
        if not s or s.type not in ("super", "normal"):
            return False
        if not _can_access(_effective_clearance(s), tier):
            return False
        if s.type == "normal" and not _provider_seal_ok(s, tier):
            return False   # normal: provider DEVE essere ≥ tier
        return True

    ai = [s for s in specs if eligible(s)]
    if tagged:
        t = next((s for s in ai if s.name == tagged), None)
        if t:
            return t
    return rank_mod.highest(ai)


def _fmt_msg(m: dict) -> str:
    """Riga di storico; rende espliciti gli allegati così l'agente sa che
    esistono file da leggere (path relativo files/<nome>)."""
    line = f"@{m.get('author', '?')}: {m.get('text', '') or ''}".rstrip()
    atts = m.get("attachments") or []
    if atts:
        line += " " + " ".join(f"[allegato: files/{a}]" for a in atts)
    return line


def _channel_files_hint(tier: str, name: str) -> str:
    return (f"I file caricati nel canale stanno in files/. Per vederli usa il tool "
            f"topic.files e per leggerne il contenuto topic.read_file con "
            f'tier="{tier}", name="{name}" (es. path "files/nomefile").')


# Capacità UI del canale: l'interfaccia trasforma marcatori-commento invisibili
# in pill cliccabili. L'agente DEVE conoscerli per offrire scelte rapide.
_CHANNEL_CAPS = (
    "Quando proponi all'utente una scelta tra opzioni, includi nel messaggio un "
    "marcatore HTML-commento (resta INVISIBILE nel testo, l'interfaccia lo rende "
    "come pill cliccabili):\n"
    "- scelta singola: <!-- choices=Opzione A,Opzione B,Opzione C --> "
    "(un click invia subito quella scelta);\n"
    "- scelta multipla: <!-- choices-multi=A,B,C --> "
    "(l'utente ne seleziona più d'una e conferma).\n"
    "Metti comunque la domanda in chiaro nel testo; il marcatore è in AGGIUNTA."
)


def _history_prompt(name: str, tier: str, messages: list[dict]) -> str:
    lines = [_fmt_msg(m) for m in messages[-15:]]
    return (f"[Canale #{name} · {tier}] Sei un partecipante. "
            + _channel_files_hint(tier, name) + "\n\n" + _CHANNEL_CAPS
            + "\n\nStorico recente:\n"
            + "\n".join(lines)
            + "\n\nRispondi all'ultimo messaggio come parte della conversazione del canale.")


def _context_messages(messages: list[dict]) -> list[dict]:
    """Solo i messaggi successivi all'ultimo reset contesto entrano nel prompt."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i] or {}
        if msg.get("kind") == "system" and msg.get("text") == "__CLODIA_CONTEXT_RESET__":
            return messages[i + 1:]
    return messages


async def _drop_channel_sessions(tier: str, name: str, participants: list[str]) -> list[str]:
    """Dimentica le sessioni runtime dei responder di questo canale."""
    deleted: list[str] = []
    for agent in participants:
        chat_id = f"chan:{tier}:{name}:{agent}"
        try:
            await manager.delete(chat_id)
            deleted.append(chat_id)
        except KeyError:
            continue
    return deleted


@router.post("/clodia/channels/{tier}/{name}/post")
async def channel_post(tier: str, name: str, req: MessageRequest, request: Request,
                       respond: bool = True) -> dict:
    """Posta un messaggio umano nel canale e fa rispondere l'agente designato."""
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto per scrivere nel canale")
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    meta = topic.get("meta", {})
    tier_real = meta.get("tier", tier)
    participants = meta.get("participants", [])
    if principal not in participants and principal != meta.get("owner"):
        raise HTTPException(403, "non sei partecipante di questo canale")

    # 1. registra il messaggio umano nel canale
    topics_client.post_message(tier, name, principal, req.content, kind="human")
    access_log.touch(tier, name)  # last_accessed → ordinamento lista Topics
    # log dell'azione umana nella sua tab Logs (gli umani non eseguono turni)
    activity_log.append(principal, "message_sent",
                        {"channel": f"{tier}/{name}",
                         "text": " ".join((req.content or "").split())[:160]})
    if not respond:
        return {"posted": True, "responder": None}

    # 2. scegli il risponditore (tag o rango più alto, con clearance)
    responder = _pick_responder(participants, tier_real, _tagged(req.content))
    if responder is None:
        return {"posted": True, "responder": None,
                "note": "nessun agente AI partecipante con clearance e provider "
                        f"adeguati al tier {tier_real} del topic"}

    # Eccezione super-agent: clodia risponde anche se il suo provider è sotto il
    # tier (per i normal sarebbe stato escluso da _pick_responder). In quel caso
    # avvisiamo lo user — la UI mostra un popup che suggerisce di attivare un altro
    # agente o un provider con SEAL ≥ tier.
    warning = None
    if responder.type == "super" and not _provider_seal_ok(responder, tier_real):
        from ..sdk_runtime.session import agent_effective_provider
        from .providers import provider_seal
        pid = agent_effective_provider(responder.name)
        warning = {
            "kind": "provider_below_tier",
            "tier": tier_real,
            "responder": responder.name,
            "provider": pid,
            "provider_seal": provider_seal(pid),
            "message": (f"Il provider in uso da {responder.name} "
                        f"({pid or 'n/d'}, {provider_seal(pid) or 'SEAL n/d'}) è "
                        f"sotto il tier {tier_real} di questo topic. I dati qui "
                        f"trattati richiederebbero un provider con SEAL ≥ {tier_real}."),
            "suggestions": [
                "Attiva un provider con SEAL ≥ tier (es. aws-region-eu o scaleway) "
                "nella sezione Providers",
                "Coinvolgi un agente il cui provider effettivo soddisfi il tier",
            ],
        }

    # 3. turno: sessione persistente per (canale, responder), riuso del runtime
    chat_id = f"chan:{tier}:{name}:{responder.name}"
    created = False
    try:
        chat = manager.get(chat_id)
    except KeyError:
        try:
            chat = await manager.create(chat_id=chat_id, kind=responder.name)
            created = True
        except ProviderNotConnected as e:
            raise HTTPException(409, str(e))
    chat.principal = principal
    # primo turno: dai il contesto del canale; poi solo il nuovo messaggio (l'SDK
    # mantiene il filo del risponditore).
    if created:
        prompt = _history_prompt(name, tier_real, _context_messages(topics_client.list_messages(tier, name, limit=200)))
    else:
        prompt = (f"[Canale #{name} · {tier_real}] @{principal}: {req.content}\n"
                  f"({_channel_files_hint(tier_real, name)} "
                  f"Per offrire scelte rapide usa <!-- choices=A,B,C --> o "
                  f"<!-- choices-multi=A,B,C -->.)")
    # indicatore "sta scrivendo…" per la UI (via SSE /clodia/events)
    await _typing(tier, name, responder.name, "start")
    try:
        reply = await chat.send_user_message(prompt)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"errore del risponditore: {str(e)[:160]}")
    finally:
        await _typing(tier, name, responder.name, "stop")

    # 4. posta la risposta nel canale
    topics_client.post_message(tier, name, responder.name, reply, kind="ai")
    return {"posted": True, "responder": responder.name, "reply": reply,
            "warning": warning}


async def run_topic_turn(tier: str, name: str, meta: dict,
                         trigger_text: str = "", principal_hint: str | None = None):
    """Esegue UN turno del responder del topic sul contesto corrente e posta la
    risposta (kind=ai). Ritorna (responder_name, reply) o (None, None).

    Usato dall'adapter dei channel esterni (Telegram): non c'è un principal umano
    → la sessione riceve un principal-hint NON privilegiato (proxy), così un
    messaggio arrivato dal canale non eredita autorità (barriera azioni, spec §5).
    Il responder è comunque scelto con le stesse regole SEAL/clearance della webui."""
    tier_real = meta.get("tier", tier)
    participants = meta.get("participants", [])
    responder = _pick_responder(participants, tier_real, _tagged(trigger_text or ""))
    if responder is None:
        return None, None
    chat_id = f"chan:{tier}:{name}:{responder.name}"
    created = False
    try:
        chat = manager.get(chat_id)
    except KeyError:
        try:
            chat = await manager.create(chat_id=chat_id, kind=responder.name)
            created = True
        except ProviderNotConnected:
            return None, None
    chat.principal = principal_hint or "channel"  # proxy: nessuna autorità
    if created:
        prompt = _history_prompt(name, tier_real,
                                 _context_messages(topics_client.list_messages(tier, name, limit=200)))
    else:
        prompt = (f"[Canale #{name} · {tier_real}] nuovo messaggio nel gruppo. "
                  f"{_channel_files_hint(tier_real, name)}")
    await _typing(tier, name, responder.name, "start")
    try:
        reply = await chat.send_user_message(prompt)
    finally:
        await _typing(tier, name, responder.name, "stop")
    topics_client.post_message(tier, name, responder.name, reply, kind="ai")
    return responder.name, reply


@router.post("/clodia/channels")
async def channel_create(request: Request) -> dict:
    """Crea un nuovo canale/topic: l'owner è l'utente connesso; come partecipante
    iniziale si aggiunge anche il contact agent richiesto, default `clodia`
    (così c'è sempre un risponditore)."""
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    body = await request.json()
    name = (body.get("name") or "").strip().lower()
    tier = _norm(body.get("tier"))
    if not name:
        raise HTTPException(400, "nome richiesto")
    meta = _channel_meta(body, principal, name)
    try:
        created = topics_client.create_topic(tier, name, meta)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"creazione canale fallita: {str(e)[:160]}")
    return {"tier": tier, "name": name, "meta": created}


@router.post("/clodia/dms")
async def dm_create(request: Request) -> dict:
    """Crea (o riapre) un DM = canale a 2 con l'utente/agent indicato in `with`.
    Idempotente: il nome è deterministico, quindi riaprire ritorna lo stesso DM."""
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    body = await request.json()
    other = (body.get("with") or "").strip().lower()
    if not other:
        raise HTTPException(400, "campo 'with' richiesto")
    if other == principal:
        raise HTTPException(400, "non puoi aprire un DM con te stesso")
    name = _dm_name(principal, other)
    meta = {
        "title": f"{principal} ↔ {other}",
        "type": "dm",
        "kind": "dm",
        "owner": principal,
        "participants": list(dict.fromkeys([principal, other])),
        "contact_agent": other,
    }
    try:
        created = topics_client.create_topic(_DM_TIER, name, meta)
    except topics_client.TopicsClientError as e:
        raise HTTPException(502, f"creazione DM fallita: {str(e)[:160]}")
    return {"tier": _DM_TIER, "name": name, "meta": created}


def _require_member(request: Request, meta: dict) -> str:
    """Solo i partecipanti (o l'owner) possono leggere/scrivere nel canale.
    Niente accesso in lettura per chi non è stato invitato (regola di owner)."""
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    if principal != meta.get("owner") and principal not in meta.get("participants", []):
        raise HTTPException(403, "non sei partecipante di questo canale")
    return principal


@router.get("/clodia/channels/{tier}/{name}/messages")
async def channel_messages(tier: str, name: str, request: Request, limit: int = 200) -> dict:
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    _require_member(request, topic.get("meta", {}))
    return {"messages": topics_client.list_messages(tier, name, limit=limit)}


@router.post("/clodia/channels/{tier}/{name}/reset-context")
async def channel_reset_context(tier: str, name: str, request: Request) -> dict:
    """Resetta il contesto conversazionale del canale.

    Non elimina i file del topic né i partecipanti: registra un marker nella
    storia e chiude le runtime session dei responder, così il prossimo turno
    riparte senza memoria conversazionale precedente.
    """
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    meta = topic.get("meta", {})
    _require_member(request, meta)
    topics_client.post_message(tier, name, principal, "__CLODIA_CONTEXT_RESET__", kind="system")
    deleted = await _drop_channel_sessions(tier, name, meta.get("participants", []))
    access_log.touch(tier, name)
    activity_log.append(principal, "channel_context_reset", {"channel": f"{tier}/{name}"})
    return {"reset": True, "sessions_deleted": deleted}


@router.get("/clodia/channels/{tier}/{name}")
async def channel_open(tier: str, name: str, request: Request) -> dict:
    """Meta del canale (owner, participants, tier, summary/tldr) per la UI.
    Solo i partecipanti/owner possono aprirlo."""
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    _require_member(request, topic.get("meta", {}))
    access_log.touch(tier, name)  # last_accessed → ordinamento lista Topics
    return topic


@router.get("/clodia/channels/{tier}/{name}/eligibility")
async def channel_eligibility(tier: str, name: str, request: Request) -> dict:
    """Idoneità di ogni AeI registrato rispetto al tier del topic.
    Usato dalla UI per (a) nascondere i partecipanti non idonei — tranne i super,
    mostrati con ⚠️ — e (b) filtrare il dropdown «aggiungi agente»."""
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    meta = topic.get("meta", {})
    _require_member(request, meta)
    tier_real = meta.get("tier", tier)
    agents = []
    for spec in registry.list():
        e = _eligibility(spec, tier_real)
        agents.append({"name": spec.name, "type": spec.type, **e})
    return {"tier": tier_real, "agents": agents}


def _require_owner(request: Request, meta: dict) -> str:
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    if principal != meta.get("owner"):
        raise HTTPException(403, "solo l'owner del canale può gestire i partecipanti")
    return principal


@router.post("/clodia/channels/{tier}/{name}/participants")
async def channel_add_participant(tier: str, name: str, request: Request) -> dict:
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    _require_owner(request, topic.get("meta", {}))
    body = await request.json()
    agent = (body.get("agent") or "").strip()
    if not agent:
        raise HTTPException(400, "agent richiesto")
    # No partecipanti inesistenti: dev'essere un agent/umano registrato.
    if registry.get_by_name(agent) is None:
        raise HTTPException(404, f"'{agent}' non esiste: invita un agent/utente registrato")
    return topics_client.set_participant(tier, name, agent, add=True)


@router.delete("/clodia/channels/{tier}/{name}/participants")
async def channel_remove_participant(tier: str, name: str, request: Request) -> dict:
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    _require_owner(request, topic.get("meta", {}))
    body = await request.json()
    agent = (body.get("agent") or "").strip()
    if not agent:
        raise HTTPException(400, "agent richiesto")
    return topics_client.set_participant(tier, name, agent, add=False)


@router.get("/clodia/channels/{tier}/{name}/files")
async def channel_files(tier: str, name: str, request: Request, path: str = "") -> dict:
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    _require_member(request, topic.get("meta", {}))
    return {"files": topics_client.list_files(tier, name, path)}


@router.post("/clodia/channels/{tier}/{name}/files")
async def channel_upload(tier: str, name: str, request: Request) -> dict:
    """Upload file nel canale (umano partecipante). Body: {filename, content_b64}."""
    topic = topics_client.open_topic(tier, name)
    if not topic:
        raise HTTPException(404, "canale non trovato")
    meta = topic.get("meta", {})
    principal = _principal_from_request(request)
    if not principal or (principal not in meta.get("participants", [])
                         and principal != meta.get("owner")):
        raise HTTPException(403, "non sei partecipante di questo canale")
    body = await request.json()
    fn = (body.get("filename") or "").strip()
    if not fn or not body.get("content_b64"):
        raise HTTPException(400, "filename e content_b64 richiesti")
    result = topics_client.put_file(tier, name, fn, body["content_b64"])
    # 1. rendi l'allegato visibile nello stream del canale (bolla con allegato)
    try:
        topics_client.post_message(tier, name, principal, "", kind="human",
                                   attachments=[fn])
    except topics_client.TopicsClientError as e:
        LOG.warning("post messaggio-allegato fallito su %s/%s: %s", tier, name, e)
    # 2. log dell'azione nella tab Logs dell'uploader
    activity_log.append(principal, "file_uploaded",
                        {"channel": f"{tier}/{name}", "file": fn})
    return result
