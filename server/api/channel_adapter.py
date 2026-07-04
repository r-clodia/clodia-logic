"""Channel-adapter Telegram (server-side, in codice — no logica AI nel trasporto).

Ciclo periodico (chiamato da un loop asyncio in main): per ogni topic con
`meta.channel.type == telegram`:
  1. INBOUND: drena i messaggi dal gateway (`telegram_client.updates`) e li posta
     nel topic come `kind=human`, autore = proxy `tg:<nome>` (identità effimera
     scoped al topic, spec §4bis). Dedup per `message_id`.
  2. TURNO: se c'è nuovo inbound, esegue UN turno del responder confinato
     (`channels.run_topic_turn`) che posta la risposta `kind=ai`.
  3. OUTBOUND: invia al gruppo i messaggi del topic NON originati dal channel
     (autore non `tg:*`) più recenti del cursore — cioè le risposte AI e i
     messaggi scritti dal lato webui. Anti-eco: i `tg:*` non rientrano.

Stato per-topic locale al backend (`channel-state/<tier>__<name>.json`): cursore
outbound + message_id già visti (cap). Idempotente: un crash ripete al più un
turno, non duplica l'outbound.
"""
from __future__ import annotations

import json
import logging

from ..config import data_path
from ..agents.loader import registry
from . import channel_responder, telegram_client, topics_client
from .channels import _tagged, run_topic_turn

LOG = logging.getLogger("agent-server.channel_adapter")

_SEEN_CAP = 500


def _state_dir():
    d = data_path("channel-state")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(tier: str, name: str):
    safe = f"{tier}__{name}".replace("/", "_")
    return _state_dir() / f"{safe}.json"


def _load_state(tier: str, name: str) -> dict:
    p = _state_path(tier, name)
    if not p.is_file():
        return {"out_cursor": "", "seen": []}
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        s.setdefault("out_cursor", "")
        s.setdefault("seen", [])
        return s
    except (OSError, json.JSONDecodeError):
        return {"out_cursor": "", "seen": []}


def _save_state(tier: str, name: str, state: dict) -> None:
    state["seen"] = state.get("seen", [])[-_SEEN_CAP:]
    _state_path(tier, name).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _proxy_author(msg: dict) -> str:
    """Identità effimera del mittente Telegram: `tg:<nome|id>` (scoped al topic)."""
    who = msg.get("from") or msg.get("from_id") or "?"
    return f"tg:{who}"


async def _mirror_topic(tier: str, name: str, channel: dict) -> None:
    chat_id = channel.get("chat_id")
    if not chat_id:
        return
    state = _load_state(tier, name)
    seen = set(state.get("seen", []))

    # Auto-provisioning del responder confinato (clone per-topic). Idempotente e
    # chiamato a ogni tick: clona se manca, e RI-registra sempre nel gateway (il
    # config.yaml del gateway è baked → un rebuild lo azzera; così si auto-guarisce).
    try:
        rn = channel_responder.ensure_responder(tier, name, tier)
        if state.get("responder") != rn:
            state["responder"] = rn
            _save_state(tier, name, state)
    except Exception as e:  # noqa: BLE001
        LOG.warning("ensure_responder %s/%s: %s", tier, name, e)

    # meta del topic: serve per sapere quali agenti sono partecipanti (un @tag
    # innesca un turno solo se indirizza un agente reale del topic).
    try:
        meta = topics_client.open_topic(tier, name).get("meta", {})
    except Exception as e:  # noqa: BLE001
        LOG.warning("open_topic %s/%s: %s", tier, name, e)
        meta = {}
    participants = set(meta.get("participants", []))

    # 1) INBOUND
    try:
        res = telegram_client.updates(chat_id)
    except Exception as e:  # noqa: BLE001
        LOG.warning("telegram updates %s/%s: %s", tier, name, e)
        return
    # Un COMANDO è un messaggio di un PRINCIPAL registrato che TAGGA un agente
    # partecipante. I proxy (mittenti non registrati su Clodia) vengono specchiati
    # ma IGNORATI come committenti: non innescano turni (modello di rango,
    # spec-rank-model §2 — gli agenti ignorano i proxy).
    command = None  # (principal_name, trigger_text) del comando più recente
    for m in res.get("messages", []):
        mid = m.get("message_id")
        if mid in seen:
            continue
        seen.add(mid)
        state["seen"].append(mid)
        text = (m.get("text") or "").strip()
        if not text:
            continue
        # Mirror come proxy `tg:<...>` (fedeltà della chat + anti-eco in outbound).
        try:
            topics_client.post_message(tier, name, _proxy_author(m), text, kind="human")
        except Exception as e:  # noqa: BLE001
            LOG.warning("post_message inbound %s/%s: %s", tier, name, e)
            continue
        # Risoluzione del mittente su identificatori STABILI (username/id), non
        # sulla stringa di display `from` (nome profilo, mutabile e non univoco).
        fid = m.get("from_id")
        sender = registry.get_by_telegram(
            m.get("from_username") or (str(fid) if fid is not None else None))
        tag = _tagged(text)
        if sender is not None and tag and tag in participants:
            command = (sender.name, text)

    # 2) TURNO — solo su comando di un principal registrato (mai su un proxy).
    # `principal_hint` = il principal REALE → il turno agisce per suo conto e il
    # gate del gateway (F2) validerà il rango del committente.
    if command:
        principal_name, trigger_text = command
        try:
            await run_topic_turn(tier, name, meta,
                                 trigger_text=trigger_text,
                                 principal_hint=principal_name)
        except Exception as e:  # noqa: BLE001
            LOG.warning("responder turn %s/%s: %s", tier, name, e)

    # 3) OUTBOUND: messaggi non originati dal channel, più recenti del cursore.
    cursor = state.get("out_cursor", "")
    last = cursor
    try:
        msgs = topics_client.list_messages(tier, name, limit=200)
    except Exception as e:  # noqa: BLE001
        LOG.warning("list_messages %s/%s: %s", tier, name, e)
        msgs = []
    for msg in msgs:
        mid = msg.get("id", "")
        if mid <= cursor:
            continue
        author = str(msg.get("author", ""))
        if not author.startswith("tg:"):  # non-eco: i tg:* non rientrano
            text = msg.get("text") or ""
            if text.strip():
                out = text if msg.get("kind") == "ai" else f"{author}: {text}"
                try:
                    telegram_client.send(chat_id, out)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("telegram send %s/%s: %s", tier, name, e)
                    break  # riprova al prossimo tick senza avanzare oltre
        last = mid
    state["out_cursor"] = last
    _save_state(tier, name, state)


async def tick_once() -> int:
    """Un giro su tutti i topic con channel telegram. Ritorna quanti processati."""
    try:
        rows = topics_client.list_topics()
    except Exception as e:  # noqa: BLE001
        LOG.warning("channel adapter: list_topics fallita: %s", e)
        return 0
    n = 0
    for r in rows:
        ch = r.get("channel")
        if ch and ch.get("type") == "telegram":
            await _mirror_topic(r.get("tier"), r.get("name"), ch)
            n += 1
    return n
