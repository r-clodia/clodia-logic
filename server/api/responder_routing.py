"""Routing del risponditore per RILEVANZA (embedding).

Invece di far rispondere sempre il super-agent di rango più alto (Clodia), si
instrada il messaggio all'agente specialista il cui DOMINIO matcha meglio — con
un embedding locale (MiniLM del micro-servizio eu-rag), quindi SENZA un turno
LLM di dispatch. I super-agent restano il FALLBACK quando nessuno specialista è
chiaramente pertinente.

Costo: 1 chiamata /embed per messaggio (~ms, offline); i profili degli agenti
sono vettorizzati una volta e cachati. Se /embed è irraggiungibile → None, e il
chiamante ricade sulla selezione per rango (nessuna dipendenza dura).
"""
from __future__ import annotations

import hashlib
import logging
import os
import urllib.parse
import urllib.request
import json

LOG = logging.getLogger("agent-server.responder_routing")

EMBED_URL = os.environ.get("EU_RAG_SEARCH_URL", "http://192.168.1.45:7900").rstrip("/")

# Soglie di routing (calibrabili). cosine su MiniLM multilingue normalizzato.
THRESHOLD = float(os.environ.get("RESPONDER_ROUTING_THRESHOLD", "0.35"))
MARGIN = float(os.environ.get("RESPONDER_ROUTING_MARGIN", "0.05"))

# cache profilo: {agent_name: (profile_hash, vector)}
_PROFILE_CACHE: dict[str, tuple[str, list[float]]] = {}


def embed_text(text: str) -> list[float] | None:
    """Vettore MiniLM (normalizzato) del testo via /embed, o None se non
    disponibile. Best-effort: qualunque errore → None (fallback a rango)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        url = f"{EMBED_URL}/embed?" + urllib.parse.urlencode({"text": text[:2000]})
        with urllib.request.urlopen(url, timeout=6) as r:
            data = json.loads(r.read())
        v = data.get("vector")
        return v if isinstance(v, list) and v else None
    except Exception as e:  # noqa: BLE001
        LOG.warning("embed non disponibile (%s): fallback a rango", str(e)[:100])
        return None


def _profile_text(spec) -> str:
    """Profilo-dominio dell'agente: nome + descrizione + competenze (skill)."""
    caps = getattr(spec, "capabilities", None) or []
    caps_txt = ", ".join(str(c) for c in caps if not str(c).endswith("/*"))
    return " ".join(filter(None, [
        getattr(spec, "display_name", "") or getattr(spec, "name", ""),
        (getattr(spec, "description", "") or "")[:800],
        f"Competenze: {caps_txt}" if caps_txt else "",
    ]))


def _profile_vec(spec) -> list[float] | None:
    """Vettore del profilo (cachato per nome+hash del profilo)."""
    prof = _profile_text(spec)
    h = hashlib.sha1(prof.encode("utf-8")).hexdigest()
    cached = _PROFILE_CACHE.get(spec.name)
    if cached and cached[0] == h:
        return cached[1]
    v = embed_text(prof)
    if v:
        _PROFILE_CACHE[spec.name] = (h, v)
    return v


def _cosine(a: list[float], b: list[float]) -> float:
    # vettori normalizzati → cosine == dot product
    return sum(x * y for x, y in zip(a, b))


def pick_by_relevance(specialists: list, message: str):
    """Fra gli specialisti (già filtrati per idoneità, NON super), ritorna quello
    più pertinente al messaggio se supera la soglia E batte il 2° del margine.
    Ritorna (spec, score) o None (→ il chiamante ricade sul rango)."""
    if not specialists:
        return None
    mv = embed_text(message)
    if not mv:
        return None
    scored = []
    for s in specialists:
        pv = _profile_vec(s)
        if pv:
            scored.append((s, _cosine(mv, pv)))
    if not scored:
        return None
    scored.sort(key=lambda x: x[1], reverse=True)
    best, best_score = scored[0]
    if best_score < THRESHOLD:
        return None
    if len(scored) > 1 and (best_score - scored[1][1]) < MARGIN:
        return None  # troppo vicino al 2° → ambiguo, meglio il capitano (rango)
    return best, best_score


def invalidate_cache(name: str | None = None) -> None:
    if name is None:
        _PROFILE_CACHE.clear()
    else:
        _PROFILE_CACHE.pop(name, None)
