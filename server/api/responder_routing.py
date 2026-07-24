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

# Soglie di routing (calibrabili via env). Ora su multilingual-e5-small con prefissi
# query/passage: le cosine sono più ALTE e compresse rispetto a MiniLM-paraphrase
# → soglia assoluta più alta. Valori di partenza, da rifinire con l'osservazione.
THRESHOLD = float(os.environ.get("RESPONDER_ROUTING_THRESHOLD", "0.80"))
MARGIN = float(os.environ.get("RESPONDER_ROUTING_MARGIN", "0.015"))

# cache profilo: {agent_name: (pieces_hash, [vettori per-pezzo])}
_PROFILE_CACHE: dict[str, tuple[str, list[list[float]]]] = {}


def embed_text(text: str, role: str = "query") -> list[float] | None:
    """Vettore (normalizzato) del testo via /embed_route (multilingual-e5-small,
    retrieval-tuned), o None se non disponibile → fallback a rango. `role`:
    'query' per il MESSAGGIO, 'passage' per i pezzi di PROFILO (prefissi e5)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        url = f"{EMBED_URL}/embed_route?" + urllib.parse.urlencode(
            {"text": text[:2000], "role": role})
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
        cap = str(cap)
        if cap.endswith("/*"):
            # Wildcard di pack (standard per gli agenti installati da pack):
            # espandi nelle skill reali, altrimenti il profilo perde proprio i
            # segnali di dominio più sharp (es. commercialista con SOLO
            # wildcard → score 0.08 su "bilancio provvisorio", che da slug
            # farebbe 0.80). base-pack/logic esclusi: skill di piattaforma
            # comuni a tutti gli agenti = nessun segnale discriminante.
            pack = cap[:-2]
            if pack in ("base-pack", "logic"):
                continue
            try:
                from ..agents.skill_sync import _pack_skill_names
                for skill in _pack_skill_names(pack):
                    w = _slug_words(skill)
                    if w:
                        pieces.append(w)
            except Exception:  # noqa: BLE001 — best-effort, profilo resta valido
                pass
            continue
        w = _slug_words(cap)
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
    vecs = [v for v in (embed_text(p, role="passage") for p in pieces) if v]
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


# Somiglianza minima a una correzione passata perché scatti l'override: alta, perché
# vogliamo instradare come la correzione SOLO su messaggi davvero simili (few-shot).
EXEMPLAR_HIT = float(os.environ.get("RESPONDER_EXEMPLAR_HIT", "0.88"))


def pick_by_exemplar(message: str, eligible_names: list[str]):
    """Se il messaggio somiglia (≥ EXEMPLAR_HIT) a una CORREZIONE passata il cui
    agente è oggi idoneo → (agent_name, sim). Altrimenti None. Il router impara così
    dalle correzioni dell'utente (k-NN a 1)."""
    from . import routing_feedback
    ex = routing_feedback.load_exemplars()
    if not ex:
        return None
    mv = embed_text(message, role="query")
    if not mv:
        return None
    elig = set(eligible_names or [])
    best_name, best_sim = None, 0.0
    for e in ex:
        if e["agent"] not in elig:
            continue
        sim = _cosine(mv, e["vec"])
        if sim > best_sim:
            best_name, best_sim = e["agent"], sim
    if best_name and best_sim >= EXEMPLAR_HIT:
        return best_name, round(best_sim, 3)
    return None


def invalidate_cache(name: str | None = None) -> None:
    if name is None:
        _PROFILE_CACHE.clear()
    else:
        _PROFILE_CACHE.pop(name, None)
