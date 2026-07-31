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
from datetime import datetime, timezone

LOG = logging.getLogger("agent-server.responder_routing")

EMBED_URL = os.environ.get("EU_RAG_SEARCH_URL", "http://192.168.1.45:7900").rstrip("/")

# Soglie di routing (calibrabili via env). Ora su multilingual-e5-small con prefissi
# query/passage: le cosine sono più ALTE e compresse rispetto a MiniLM-paraphrase
# → soglia assoluta più alta. Valori di partenza, da rifinire con l'osservazione.
THRESHOLD = float(os.environ.get("RESPONDER_ROUTING_THRESHOLD", "0.80"))
MARGIN = float(os.environ.get("RESPONDER_ROUTING_MARGIN", "0.015"))
# Rapporto della soglia soft rispetto a quella hard: deve stare in (0, 1], altrimenti
# soft_threshold ≥ THRESHOLD e il multi-fallback diventa codice morto senza avviso.
FALLBACK_SOFT_RATIO = min(1.0, max(
    0.01, float(os.environ.get("RESPONDER_FALLBACK_SOFT_RATIO", "0.87"))))

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


def soft_matches(scored: list) -> list[tuple]:
    """Specialisti sopra la soglia morbida usata dal fallback multi-match."""
    soft_threshold = THRESHOLD * FALLBACK_SOFT_RATIO
    return [(spec, score) for spec, score in scored if score >= soft_threshold]


def pick_by_relevance(specialists: list, message: str):
    """Fra gli specialisti (idonei, NON super), ritorna (spec, score) del più
    pertinente se supera soglia E batte il 2° del margine, altrimenti None
    (→ fallback a rango/Clodia)."""
    return decide(score_specialists(specialists, message))


# Il classificatore degli esemplari usa un criterio RELATIVO: i top-k vicini
# votano per agente, con correzioni più forti delle conferme e decadimento nel
# tempo. Si applica solo quando il vincitore stacca il secondo in modo netto.
EXEMPLAR_K = max(1, int(os.environ.get("RESPONDER_EXEMPLAR_K", "5")))
EXEMPLAR_MARGIN = min(1.0, max(
    0.0, float(os.environ.get("RESPONDER_EXEMPLAR_MARGIN", "0.15"))))
EXEMPLAR_CONFIRM_WEIGHT = min(1.0, max(
    0.0, float(os.environ.get("RESPONDER_EXEMPLAR_CONFIRM_WEIGHT", "0.50"))))
EXEMPLAR_HALF_LIFE_DAYS = max(
    1.0, float(os.environ.get("RESPONDER_EXEMPLAR_HALF_LIFE_DAYS", "90")))

# FLOOR di similarità: il criterio relativo (margine sul secondo) da solo NON
# basta, perché un vincitore senza avversari ha margine 1.0 QUALUNQUE sia la
# similarità. Su e5 le cosine sono compresse in alto (misurato sul corpus reale:
# coppie stesso-agente 0.822 medio, coppie agenti-diversi 0.814), quindi senza
# floor il classificatore aggancia rumore: 6 frasi su 8 fuori dominio venivano
# instradate, con max_similarity ~0.79. Il floor a 0.80 azzera quei falsi
# positivi senza costare copertura in-dominio (28/39 invariata).
EXEMPLAR_FLOOR = min(1.0, max(
    0.0, float(os.environ.get("RESPONDER_EXEMPLAR_FLOOR", "0.80"))))

# MODALITÀ del classificatore:
#   "shadow"  (default) → calcola e REGISTRA la decisione che avrebbe preso, ma
#                         NON la applica: il routing resta quello per rilevanza;
#   "enforce"           → applica la decisione.
# Il default è shadow perché sul corpus attuale l'accuratezza leave-one-out è
# 21–30% (39 voti su 9 agenti): applicarla peggiorerebbe il routing. Si passa a
# "enforce" quando `GET /clodia/routing/stats` mostra un'accuratezza adeguata
# (indicativamente ≥ 70%) su copertura non banale — decisione esplicita, non
# automatica.
EXEMPLAR_MODE = (os.environ.get("RESPONDER_EXEMPLAR_MODE", "shadow") or "shadow").strip().lower()


def exemplar_enforced() -> bool:
    """True se le decisioni degli esemplari vengono APPLICATE (non solo tracciate)."""
    return (os.environ.get("RESPONDER_EXEMPLAR_MODE", EXEMPLAR_MODE)
            or "").strip().lower() == "enforce"


def _temporal_weight(timestamp: str | None,
                     now: datetime | None = None) -> float:
    if not timestamp:
        return 1.0
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 1.0
    current = now or datetime.now(timezone.utc)
    age_days = max(0.0, (current - parsed).total_seconds() / 86400)
    return 0.5 ** (age_days / EXEMPLAR_HALF_LIFE_DAYS)


def _classify_exemplar_vector(vector: list[float], exemplars: list[dict],
                               eligible_names: list[str], *,
                               now: datetime | None = None) -> dict | None:
    eligible = set(eligible_names or [])
    neighbors = []
    for exemplar in exemplars:
        if exemplar.get("agent") not in eligible or not exemplar.get("vec"):
            continue
        similarity = _cosine(vector, exemplar["vec"])
        neighbors.append((similarity, exemplar))
    neighbors.sort(key=lambda item: item[0], reverse=True)
    neighbors = neighbors[:EXEMPLAR_K]
    if not neighbors:
        return None

    scores: dict[str, float] = {}
    support: dict[str, int] = {}
    max_similarity: dict[str, float] = {}
    for similarity, exemplar in neighbors:
        agent = exemplar["agent"]
        kind_weight = (
            EXEMPLAR_CONFIRM_WEIGHT
            if exemplar.get("kind") == "confirm"
            else 1.0
        )
        weight = max(0.0, similarity) * kind_weight * _temporal_weight(
            exemplar.get("ts"), now
        )
        scores[agent] = scores.get(agent, 0.0) + weight
        support[agent] = support.get(agent, 0) + 1
        max_similarity[agent] = max(max_similarity.get(agent, -1.0), similarity)

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    winner, winner_score = ranked[0]
    runner_score = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = (
        (winner_score - runner_score) / winner_score
        if winner_score > 0 else 0.0
    )
    if confidence < EXEMPLAR_MARGIN:
        return None
    # FLOOR: il margine relativo non dice nulla sulla PERTINENZA. Senza questo
    # check un vincitore senza avversari passa con confidence 1.0 anche a
    # similarità da rumore. Il vincitore deve somigliare davvero a qualcosa che
    # l'utente ha annotato.
    if max_similarity[winner] < EXEMPLAR_FLOOR:
        return None
    return {
        "agent": winner,
        "confidence": confidence,
        "score": winner_score,
        "runner_score": runner_score,
        "support": support[winner],
        "max_similarity": max_similarity[winner],
    }


def pick_by_exemplar(message: str, eligible_names: list[str],
                     known_names: set[str] | None = None,
                     *, topic: str | None = None):
    """k-NN pesato sul feedback supervisionato → (agent, confidence) o None.

    In modalità **shadow** (default) la decisione viene calcolata e TRACCIATA ma
    NON restituita: il chiamante prosegue col routing per rilevanza. Serve ad
    accumulare evidenza — visibile su `GET /clodia/routing/stats` — prima di
    dare al classificatore il potere di dirottare il routing."""
    from . import routing_feedback
    ex = routing_feedback.load_exemplars(known_names)
    if not ex:
        return None
    mv = embed_text(message, role="query")
    if not mv:
        return None
    result = _classify_exemplar_vector(mv, ex, eligible_names)
    if result is None:
        return None
    enforced = exemplar_enforced()
    LOG.info(
        "routing exemplar (%s): agent=%s confidence=%.3f support=%d max_sim=%.3f",
        "enforce" if enforced else "shadow",
        result["agent"], result["confidence"], result["support"],
        result["max_similarity"],
    )
    try:
        routing_feedback.record_decision(
            "exemplar" if enforced else "exemplar-shadow",
            result["agent"], confidence=round(result["confidence"], 3),
            mode="exemplar", topic=topic,
        )
    except Exception as e:  # noqa: BLE001 — la telemetria non deve mai bloccare il routing
        LOG.debug("telemetria esemplare non registrata: %s", e)
    if not enforced:
        return None
    return result["agent"], round(result["confidence"], 3)


def evaluate_exemplars(exemplars: list[dict],
                       eligible_names: list[str]) -> dict:
    """Leave-one-out evaluation on the installed privacy-preserving corpus."""
    evaluated = predicted = correct = 0
    for index, exemplar in enumerate(exemplars):
        target = exemplar.get("agent")
        vector = exemplar.get("vec")
        if target not in eligible_names or not vector:
            continue
        evaluated += 1
        others = exemplars[:index] + exemplars[index + 1:]
        result = _classify_exemplar_vector(vector, others, eligible_names)
        if result is None:
            continue
        predicted += 1
        if result["agent"] == target:
            correct += 1
    return {
        "evaluated": evaluated,
        "predicted": predicted,
        "correct": correct,
        "coverage": round(predicted / evaluated, 4) if evaluated else 0.0,
        "accuracy": round(correct / predicted, 4) if predicted else None,
        "k": EXEMPLAR_K,
        "margin": EXEMPLAR_MARGIN,
    }


def invalidate_cache(name: str | None = None) -> None:
    if name is None:
        _PROFILE_CACHE.clear()
    else:
        _PROFILE_CACHE.pop(name, None)
