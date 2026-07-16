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
# Con il match MULTI-VETTORE (max sui pezzi) i picchi sono più alti → soglia più
# alta e margine più piccolo che nel vecchio profilo mediato.
THRESHOLD = float(os.environ.get("RESPONDER_ROUTING_THRESHOLD", "0.50"))
MARGIN = float(os.environ.get("RESPONDER_ROUTING_MARGIN", "0.03"))

# cache profilo: {agent_name: (pieces_hash, [vettori per-pezzo])}
_PROFILE_CACHE: dict[str, tuple[str, list[list[float]]]] = {}


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


_RAG_TITLES_CACHE: dict[str, tuple[float, list[str]]] = {}   # coll → (ts, [titoli])
_RAG_TTL = 300.0


def _rag_title_list(collection: str) -> list[str]:
    """Titoli (puliti) dei documenti di una collection RAG = knowledge base.
    Ognuno diventa un PEZZO del profilo. Cachati con TTL."""
    import time
    hit = _RAG_TITLES_CACHE.get(collection)
    if hit and (time.time() - hit[0]) < _RAG_TTL:
        return hit[1]
    titles: list[str] = []
    try:
        url = f"{EMBED_URL}/documents?" + urllib.parse.urlencode({"collection": collection})
        with urllib.request.urlopen(url, timeout=6) as r:
            docs = (json.loads(r.read()) or {}).get("documents") or []
        titles = [str(d.get("name") or "").replace("-", " ").replace("_", " ").strip()
                  for d in docs if d.get("name")]
    except Exception:  # noqa: BLE001
        titles = []
    _RAG_TITLES_CACHE[collection] = (time.time(), titles)
    return titles


def _agent_collections(spec) -> list[str]:
    """Collection RAG a cui l'agente accede: quelle dichiarate in `rag_read` più
    quelle derivate dai suoi tool (eu_corpus.*/rag.* → il corpus di piattaforma
    'eu-normativa'). Aitiero, p.es., ha rag_read vuoto ma i tool eu_corpus/rag."""
    colls = set(getattr(spec, "rag_read", None) or [])
    tp = [str(t) for t in (getattr(spec, "tool_permissions", None) or [])]
    if any(t.startswith("eu_corpus") or t.startswith("rag.") or t == "rag" for t in tp):
        colls.add("eu-normativa")
    return list(colls)


def _slug_words(cap: str) -> str:
    """Slug skill → parole di dominio (l'ultimo segmento, trattini→spazi):
    'tomato/tomato-blue-preventivo' → 'tomato blue preventivo'. Leggero e
    multilingue: le description dei SKILL.md sono spesso in inglese e verbose →
    diluiscono l'embedding e sbagliano il match sulle query italiane."""
    return cap.split("/")[-1].replace("-", " ").replace("_", " ").strip()


def _profile_pieces(spec) -> list[str]:
    """PEZZI di dominio dell'agente (per il match MULTI-VETTORE): ogni pezzo è un
    segnale sharp (una clausola dell'expertise, una skill, un titolo di documento
    RAG). Lo score dell'agente = MAX cosine su questi pezzi → l'ampiezza del
    profilo non diluisce più i picchi (una skill precisa vince quando pertinente,
    le altre restano basse). Auto-manutenuto: skill/documenti nuovi = pezzi nuovi."""
    import re
    pieces: list[str] = []
    exp = (getattr(spec, "expertise", "") or "").strip()
    if exp:
        pieces += [c.strip() for c in re.split(r"[;,.\n]", exp) if len(c.strip()) >= 4]
    for cap in (getattr(spec, "capabilities", None) or []):
        if str(cap).endswith("/*"):
            continue
        w = _slug_words(str(cap))
        if w:
            pieces.append(w)
    for coll in _agent_collections(spec):
        pieces += [t for t in _rag_title_list(str(coll)) if t]
    # Filtro qualità: scarta i pezzi RUMOROSI (acronimi/mono-parola tipo "AGA",
    # "pdf", "docx") — le stringhe corte hanno embedding inaffidabili e danno
    # match spuri (es. "AGA" ~ "ciao come va" a 0.61). Tieni frasi ≥2 parole e
    # ≥8 char; quei domini restano coperti dalle clausole dell'expertise.
    pieces = [p for p in pieces if len(p.strip()) >= 8 and len(p.split()) >= 2]
    if not pieces:   # niente segnale utile → usa la description come unico pezzo
        d = (getattr(spec, "description", "") or "")[:300].strip()
        if d:
            pieces.append(d)
    # dedup preservando l'ordine
    seen, out = set(), []
    for p in pieces:
        k = p.lower()
        if k not in seen:
            seen.add(k); out.append(p)
    return out


def _profile_vecs(spec) -> list[list[float]]:
    """Vettori dei pezzi del profilo (cachati per hash dei pezzi)."""
    pieces = _profile_pieces(spec)
    h = hashlib.sha1("".join(pieces).encode("utf-8")).hexdigest()
    cached = _PROFILE_CACHE.get(spec.name)
    if cached and cached[0] == h:
        return cached[1]
    vecs = [v for v in (embed_text(p) for p in pieces) if v]
    if vecs:
        _PROFILE_CACHE[spec.name] = (h, vecs)
    return vecs


def _cosine(a: list[float], b: list[float]) -> float:
    # vettori normalizzati → cosine == dot product
    return sum(x * y for x, y in zip(a, b))


def score_specialists(specialists: list, message: str) -> list[tuple]:
    """[(spec, score)] ordinato per rilevanza (max-sim sui pezzi del profilo).
    [] se /embed non disponibile o nessun profilo. Base sia del picker sia del
    TRACE del routing mostrato in UI."""
    if not specialists:
        return []
    mv = embed_text(message)
    if not mv:
        return []
    scored = []
    for s in specialists:
        vecs = _profile_vecs(s)
        if vecs:
            scored.append((s, max(_cosine(mv, v) for v in vecs)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def decide(scored: list):
    """Applica soglia+margine a uno scored già ordinato → (spec, score) o None."""
    if not scored:
        return None
    best, best_score = scored[0]
    if best_score < THRESHOLD:
        return None
    if len(scored) > 1 and (best_score - scored[1][1]) < MARGIN:
        return None
    return best, best_score


def pick_by_relevance(specialists: list, message: str):
    """Fra gli specialisti (idonei, NON super), ritorna (spec, score) del più
    pertinente se supera soglia E batte il 2° del margine, altrimenti None
    (→ fallback a rango/Clodia)."""
    return decide(score_specialists(specialists, message))


def invalidate_cache(name: str | None = None) -> None:
    if name is None:
        _PROFILE_CACHE.clear()
    else:
        _PROFILE_CACHE.pop(name, None)
