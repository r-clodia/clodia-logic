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
THRESHOLD = float(os.environ.get("RESPONDER_ROUTING_THRESHOLD", "0.30"))
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


_SKILL_DESC_CACHE: dict[str, str] = {}
_RAG_TITLES_CACHE: dict[str, tuple[float, str]] = {}   # collection → (ts, titoli)
_RAG_TTL = 300.0


def _skill_description(slug: str) -> str:
    """Descrizione in linguaggio naturale di una skill (dalla frontmatter del suo
    SKILL.md nel catalog), non lo slug opaco. Cachata."""
    if slug in _SKILL_DESC_CACHE:
        return _SKILL_DESC_CACHE[slug]
    desc = ""
    try:
        from ..agents.skill_sync import _resolve_skill_source
        import yaml
        src = _resolve_skill_source(slug)
        if src and (src / "SKILL.md").is_file():
            txt = (src / "SKILL.md").read_text(encoding="utf-8")
            if txt.startswith("---"):
                fm = txt.split("---", 2)[1]
                meta = yaml.safe_load(fm) or {}
                desc = str(meta.get("description") or "").strip()
    except Exception:  # noqa: BLE001
        desc = ""
    _SKILL_DESC_CACHE[slug] = desc
    return desc


def _rag_titles(collection: str) -> str:
    """Titoli dei documenti di una collection RAG (la KNOWLEDGE BASE dell'agente).
    Cachati con TTL per non interrogare /documents a ogni messaggio."""
    import time
    hit = _RAG_TITLES_CACHE.get(collection)
    if hit and (time.time() - hit[0]) < _RAG_TTL:
        return hit[1]
    titles = ""
    try:
        url = f"{EMBED_URL}/documents?" + urllib.parse.urlencode({"collection": collection})
        with urllib.request.urlopen(url, timeout=6) as r:
            docs = (json.loads(r.read()) or {}).get("documents") or []
        titles = ", ".join(str(d.get("name") or "") for d in docs if d.get("name"))
    except Exception:  # noqa: BLE001
        titles = ""
    _RAG_TITLES_CACHE[collection] = (time.time(), titles)
    return titles


def _profile_text(spec) -> str:
    """Profilo-dominio AUTO-DERIVATO da ciò che l'agente sa davvero:
    - descrizioni in linguaggio naturale delle sue SKILL (non gli slug);
    - titoli dei documenti delle sue collection RAG (`rag_read`) = knowledge base;
    - più `expertise` (frase curata) come AUGMENT opzionale.
    Auto-manutenuto: aggiungi una skill o ingesti un documento → il profilo (e il
    routing) si aggiornano da soli. Fallback: description se non c'è altro."""
    parts = [getattr(spec, "display_name", "") or spec.name]
    exp = (getattr(spec, "expertise", "") or "").strip()
    if exp:
        parts.append(exp)
    # skill → descrizioni (salta i wildcard tipo base-pack/*: generici)
    for cap in (getattr(spec, "capabilities", None) or []):
        if str(cap).endswith("/*"):
            continue
        d = _skill_description(str(cap))
        if d:
            parts.append(d)
    # knowledge base RAG → titoli documenti
    for coll in (getattr(spec, "rag_read", None) or []):
        t = _rag_titles(str(coll))
        if t:
            parts.append(f"Conosce documenti su: {t}")
    if len(parts) == 1:   # solo il nome → usa la description come fallback
        d = (getattr(spec, "description", "") or "")[:600]
        if d:
            parts.append(d)
    return ". ".join(p for p in parts if p)


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
