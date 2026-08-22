"""Persistenza dei job dello scheduler — file-per-job (clodia-data/jobs/<id>.yaml).

Sostituisce il vecchio SQLite (agent-state/jobs.db): i job sono ora file YAML
**editabili** e **clonabili** (un clone nuovo parte con jobs/ vuoto). Stessa
interfaccia del modulo precedente, così api.py e scheduler.py restano invariati.

Gerarchia seed → job → spawn: il job è la definizione durevole di lavoro
schedulato; quando parte materializza uno spawn dell'executor.

Schema job (jobs/<id>.yaml):
    id, name, cron_expr, prompt, agent, enabled, tier, last_run_at, last_status,
    last_chat_id, topic_tier, topic_name, created_at, updated_at

Periodicità — due forme, mai entrambe attive:
    - `cron_expr`: espressione a 5 campi (job globali, e trigger di topic creati
      prima di clodia-platform#239).
    - `interval_minutes` + `repeat_count`: «ogni N minuti, per M volte» (trigger
      di topic dal #239). `repeat_count = 0` = senza fine; `fired_count` conta i
      fire effettivamente dispatchati, e quando raggiunge `repeat_count` il job
      si disabilita da sé.
    Un record con `interval_minutes` valorizzato ignora `cron_expr`: la
    conversione dei trigger legacy NON è automatica, la fa l'owner salvando dal
    pannello (cambiare un orario di fire in silenzio è peggio del campo vecchio).

`agent` = nome dell'agent (kind) che lo scheduler spawna al fire del job;
risolto dinamicamente (statico clodia/ada/looper/ophelia o seed del registry).
Job creati prima dell'introduzione del campo (19 giu 2026) → default "looper"
in lettura, per preservarne il comportamento storico.

**UN** nome, mai una lista (R11 del router notebook, clodia-platform#213): uno
scope asincrono ha un solo responder, ed è su quella certezza che il router si
permette di non girare mai su un job. In scrittura una lista è RIFIUTATA
(`create_job`/`update_job` → ValueError); in lettura è coercizzata al primo nome
con un warning, perché questi file si editano a mano e far sparire da
`list_jobs()` un job programmato sarebbe un guasto peggiore di quello curato.
"""
import logging
import sqlite3  # solo per IntegrityError: contratto con api.py sul nome duplicato
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml

from ..config import data_path

# Directory persistente dei job sotto CLODIA_DATA (volume montato).
JOBS_DIR = data_path("jobs")

_FIELDS = (
    "id", "name", "cron_expr", "prompt", "agent", "enabled", "owner",
    "mode", "plan", "tier", "topic_tier", "topic_name", "runs", "run_seq",
    "interval_minutes", "repeat_count", "fired_count",
    "last_run_at", "last_status", "last_chat_id", "created_at", "updated_at",
)

# Agent di fallback per job senza il campo `agent` (creati prima del 19 giu 2026).
_LEGACY_DEFAULT_AGENT = "looper"

LOG = logging.getLogger("scheduler.db")


def _one_agent_name(agent):
    """Guardia di scrittura per R11: `agent` è UN nome, o niente.

    `None` passa (= «non mi pronuncio»: default in `create_job`, campo non
    toccato in `update_job`) e `""` resta lecito (job `logic`/`topic_trigger`,
    che non hanno responder). Tutto il resto — lista, tupla, dict — è rifiutato
    qui, un solo punto per entrambe le porte di scrittura, invece di una guardia
    per chiamante: `create_topic_trigger` e `proposals.approve` la ereditano.
    """
    if agent is None or isinstance(agent, str):
        return agent
    raise ValueError(
        f"R11: un job ha UN agente, non {type(agent).__name__} ({agent!r}). "
        "Uno scope asincrono con due responder non ha una regola su chi "
        "risponde: se il campo va allargato, va deciso — non subito "
        "(clodia-platform#213).")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(job_id: int) -> Path:
    return JOBS_DIR / f"{job_id}.yaml"


def init_db() -> None:
    """Crea la directory jobs/ se non esiste (no-op se già presente)."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _read(p: Path) -> Optional[dict]:
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if "id" not in d:
        return None
    d["enabled"] = bool(d.get("enabled", True))
    d["mode"] = d.get("mode") or "agentic"
    # Lettura tollerante, scrittura severa: il file può essere stato scritto a
    # mano (vedi intestazione), quindi qui la lista non la rifiuto — il job
    # sparirebbe da list_jobs() e non partirebbe più, che è peggio. Prendo il
    # primo nome e lo dico nel log: in scrittura c'è una persona che legge
    # l'errore, qui no.
    if isinstance(d.get("agent"), (list, tuple)):
        _lista = list(d["agent"])
        d["agent"] = str(_lista[0]) if _lista else ""
        LOG.warning(
            "job %s (%s): campo `agent` con %d nomi %r → uso il primo (%r). "
            "R11: un job ha UN agente (clodia-platform#213).",
            d.get("id"), d.get("name"), len(_lista), _lista, d["agent"])
    # Default legacy agent SOLO per i job agentici: un job LOGICO non ha agent
    # (nessun turno LLM) → resta vuoto, non coercizzato a 'looper'.
    if d["mode"] in ("logic", "topic_trigger"):
        d["agent"] = d.get("agent") or ""
    else:
        d["agent"] = d.get("agent") or _LEGACY_DEFAULT_AGENT
    # Job legacy (pre-owner) → owner vuoto = di sistema: solo un admin può agirvi.
    d["owner"] = d.get("owner") or ""
    # Periodicità a intervallo (#239). Assente = job a cron: `None`, non 0, così
    # `register_job` distingue «non usa l'intervallo» da «intervallo nullo».
    d["interval_minutes"] = _pos_int_or_none(d.get("interval_minutes"))
    d["repeat_count"] = _non_neg_int(d.get("repeat_count"))
    d["fired_count"] = _non_neg_int(d.get("fired_count"))
    return d


def _pos_int_or_none(v) -> Optional[int]:
    """Intero > 0, oppure None. Lettura tollerante come il resto di `_read`: un
    file scritto a mano con `interval_minutes: ""` non deve far sparire il job."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _non_neg_int(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _write(d: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {k: d.get(k) for k in _FIELDS}
    _path(d["id"]).write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _all() -> list[dict]:
    if not JOBS_DIR.is_dir():
        return []
    out = []
    for p in sorted(JOBS_DIR.glob("*.yaml")):
        d = _read(p)
        if d is not None:
            out.append(d)
    return sorted(out, key=lambda j: j["id"])


def _next_id() -> int:
    ids = [j["id"] for j in _all() if isinstance(j.get("id"), int)]
    return (max(ids) + 1) if ids else 1


def create_job(name: str, cron_expr: str, prompt: str,
               agent: str = "clodia", enabled: bool = True,
               owner: str = "", mode: str = "agentic",
               plan: list | None = None, tier: str = "",
               topic_tier: str = "", topic_name: str = "",
               interval_minutes: Optional[int] = None,
               repeat_count: int = 0) -> dict:
    """Crea un nuovo job. Solleva sqlite3.IntegrityError se 'name' è duplicato
    (contratto invariato con api.py → HTTP 409). `owner` = principal umano che ne
    è proprietario (solo lui, o un admin, può agirvi).

    `mode`: 'agentic' (default, turno LLM sul `prompt`) o 'logic' (esegue `plan`,
    lista di {verb, args}, senza LLM né gate — pre-autorizzato dalla creazione).

    Solleva ValueError se `agent` non è un nome solo (R11, clodia-platform#213)."""
    agent = _one_agent_name(agent)   # R11: un job ha UN agente (#213)
    if get_job_by_name(name) is not None:
        raise sqlite3.IntegrityError(f"job name '{name}' already exists")
    now = _now_iso()
    _mode = mode or "agentic"
    d = {
        "id": _next_id(), "name": name, "cron_expr": cron_expr, "prompt": prompt,
        # Job logico/topic → agent opzionale o assente; agentico → agent o clodia.
        "agent": (agent or "") if _mode in ("logic", "topic_trigger")
                 else (agent or "clodia"),
        "owner": owner or "",
        "mode": _mode, "plan": plan or [],
        # Tier RICHIESTO dal job: il livello dei dati che tratterà. Assente = non
        # dichiarato = nessun requisito, che è il comportamento di ogni job
        # esistente. Un default diverso da "" farebbe fallire al primo fire job
        # che oggi girano.
        "tier": tier or "",
        "topic_tier": topic_tier or "", "topic_name": topic_name or "",
        # Periodicità a intervallo (#239): alternativa a `cron_expr`, non aggiunta.
        "interval_minutes": _pos_int_or_none(interval_minutes),
        "repeat_count": _non_neg_int(repeat_count), "fired_count": 0,
        "enabled": bool(enabled), "last_run_at": None, "last_status": None,
        "last_chat_id": None, "created_at": now, "updated_at": now,
    }
    _write(d)
    return d


def get_job(job_id: int) -> Optional[dict]:
    p = _path(job_id)
    return _read(p) if p.is_file() else None


def get_job_by_name(name: str) -> Optional[dict]:
    for j in _all():
        if j.get("name") == name:
            return j
    return None


def list_jobs() -> list[dict]:
    return _all()


def get_topic_trigger(tier: str, name: str) -> Optional[dict]:
    """Return the single cron trigger attached to a topic, if configured."""
    return next((
        job for job in _all()
        if job.get("mode") == "topic_trigger"
        and job.get("topic_tier") == tier
        and job.get("topic_name") == name
    ), None)


def create_topic_trigger(
    tier: str,
    name: str,
    prompt: str,
    *,
    interval_minutes: int,
    repeat_count: int = 0,
    agent: str = "",
    owner: str = "",
) -> dict:
    """Crea il trigger di un topic: «ogni `interval_minutes`, per `repeat_count`
    volte» (`repeat_count = 0` = senza fine, #239).

    Non prende più un `cron_expr`: i trigger nuovi nascono a intervallo. I
    record legacy che ce l'hanno continuano a girare (vedi `scheduler.
    register_job`), ma la porta di creazione non ne produce di nuovi."""
    if get_topic_trigger(tier, name) is not None:
        raise sqlite3.IntegrityError(f"topic trigger '{tier}/{name}' already exists")
    return create_job(
        name=f"topic-trigger:{tier}/{name}"[:200],
        cron_expr="",
        prompt=prompt,
        agent=agent,
        owner=owner,
        mode="topic_trigger",
        topic_tier=tier,
        topic_name=name,
        interval_minutes=interval_minutes,
        repeat_count=repeat_count,
    )


def update_job(
    job_id: int,
    *,
    name: Optional[str] = None,
    cron_expr: Optional[str] = None,
    prompt: Optional[str] = None,
    agent: Optional[str] = None,
    enabled: Optional[bool] = None,
    tier: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    repeat_count: Optional[int] = None,
    fired_count: Optional[int] = None,
) -> Optional[dict]:
    """Aggiorna i campi non None. Ritorna il job aggiornato o None se non esiste.

    Passare `interval_minutes` sposta il job dalla periodicità cron a quella a
    intervallo e AZZERA `cron_expr`: le due forme non convivono, un record con
    entrambe lascerebbe a `register_job` una scelta che non gli compete."""
    agent = _one_agent_name(agent)   # R11: prima di toccare il file (#213)
    d = get_job(job_id)
    if d is None:
        return None
    if name is not None:
        other = get_job_by_name(name)
        if other is not None and other["id"] != job_id:
            raise sqlite3.IntegrityError(f"job name '{name}' already exists")
        d["name"] = name
    if cron_expr is not None:
        d["cron_expr"] = cron_expr
        if cron_expr:
            d["interval_minutes"] = None
    if interval_minutes is not None:
        nuovo = _pos_int_or_none(interval_minutes)
        # Cambiare la cadenza ri-arma il conteggio: «ogni 30' per 4 volte» dopo
        # una modifica sono 4 volte NUOVE, non le 4 meno quelle già spese.
        if nuovo != d.get("interval_minutes"):
            d["fired_count"] = 0
        d["interval_minutes"] = nuovo
        d["cron_expr"] = ""
    if repeat_count is not None:
        nuovo_rc = _non_neg_int(repeat_count)
        if nuovo_rc != d.get("repeat_count"):
            d["fired_count"] = 0
        d["repeat_count"] = nuovo_rc
    if fired_count is not None:
        d["fired_count"] = _non_neg_int(fired_count)
    if prompt is not None:
        d["prompt"] = prompt
    if agent is not None:
        d["agent"] = agent
    if enabled is not None:
        d["enabled"] = bool(enabled)
    if tier is not None:
        # `""` TOGLIE il requisito, `None` non si pronuncia. Senza la
        # distinzione un tier dichiarato per errore non si potrebbe più
        # rimuovere se non riscrivendo il file a mano.
        d["tier"] = tier
    d["updated_at"] = _now_iso()
    _write(d)
    return d


def delete_job(job_id: int) -> bool:
    """Ritorna True se ha cancellato qualcosa, False se non esisteva."""
    p = _path(job_id)
    if p.is_file():
        p.unlink()
        return True
    return False


_RUNS_CAP = 100  # storico run tenuto per job (i più recenti)


def mark_run(job_id: int, *, status: str, chat_id: Optional[str] = None) -> Optional[str]:
    """Registra un fire nello STORICO run del job (`runs`) e aggiorna i campi
    `last_*`. Ritorna l'id del run, usato dal completamento asincrono per
    aggiornare la stessa entry. Lo storico è cappato agli ultimi _RUNS_CAP."""
    d = get_job(job_id)
    if d is None:
        return None
    ts = _now_iso()
    s = str(status)
    if s.startswith(("error", "failed")):
        stato = "failed"
    elif s.startswith("dispatched"):
        stato = "running"
    elif s.startswith(("ok", "success", "completed")):
        stato = "success"
    else:
        stato = s.split(" ", 1)[0]
    seq = int(d.get("run_seq") or 0) + 1
    entry = {
        "id": str(seq),
        "ts": ts,
        "stato": stato,
        "chat_id": chat_id,
        "error": s.split(":", 1)[-1].strip() or s if stato == "failed" else None,
        "note": s if stato == "running" else None,
    }
    runs = list(d.get("runs") or [])
    runs.append(entry)
    d["run_seq"] = seq
    d["runs"] = runs[-_RUNS_CAP:]
    d["last_run_at"] = ts
    d["last_status"] = status
    d["last_chat_id"] = chat_id
    d["updated_at"] = ts
    _write(d)
    return entry["id"]


#: Stati terminali di un run. `success`/`error`/`fatal` li dichiara l'AGENTE
#: (`scheduler.run_status`), `failed` lo constata l'infrastruttura quando il
#: turno muore. Insieme chiuso perché su questi stati la UI dipinge un pallino:
#: uno stato inventato diventa `unknown` e si legge come «boh» esattamente dove
#: serviva una risposta.
TERMINAL_STATES = ("success", "error", "fatal", "failed")

#: Stati terminali che NON sono un successo pieno. Serve a chi legge lo storico
#: per sapere cosa merita un'occhiata, senza enumerare a mano tre stringhe in
#: ogni chiamante — e la quarta nascerebbe senza.
NOT_OK = ("error", "fatal", "failed")


def complete_run(job_id: int, run_id: str, *, status: Optional[str] = None,
                 success: Optional[bool] = None,
                 error: Optional[str] = None) -> bool:
    """Porta uno specifico run agentico a stato terminale e ne persiste durata.

    L'id evita che il completamento tardivo di un run aggiorni per errore una
    esecuzione successiva dello stesso job.

    `status` è uno di `TERMINAL_STATES`. `success: bool` resta accettato per i
    chiamanti che conoscono solo la vecchia forma binaria — mappato su
    `success`/`failed` — perché il punto della modifica è che uno stato in più
    esista, non che ogni chiamante debba cambiare nello stesso commit. Passarli
    entrambi è un errore del chiamante, non una precedenza da indovinare.
    """
    if status is None and success is None:
        raise ValueError("complete_run richiede `status` oppure `success`")
    if status is not None and success is not None:
        raise ValueError(
            "complete_run: passa `status` OPPURE `success`, non entrambi "
            f"(status={status!r}, success={success!r})")
    if status is None:
        status = "success" if success else "failed"
    status = str(status).strip().lower()
    if status not in TERMINAL_STATES:
        raise ValueError(
            f"stato terminale '{status}' non valido; ammessi: "
            f"{', '.join(TERMINAL_STATES)}")
    d = get_job(job_id)
    if d is None:
        return False
    runs = list(d.get("runs") or [])
    entry = next((row for row in runs if str(row.get("id")) == str(run_id)), None)
    if entry is None:
        return False
    finished_at = _now_iso()
    try:
        started = datetime.fromisoformat(str(entry.get("ts")))
        finished = datetime.fromisoformat(finished_at)
        duration = max(0.0, (finished - started).total_seconds())
    except (TypeError, ValueError):
        duration = None
    # Il DETTAGLIO non è più «l'errore»: su `error` il lavoro è stato consegnato e
    # il testo dice cosa può averne compromesso la qualità. Il campo si chiama
    # ancora `error` perché è quello che la UI legge già; cambiargli nome qui
    # avrebbe fatto sparire il testo dallo schermo senza che nulla segnalasse il
    # perché.
    dettaglio = str(error).strip() if error not in (None, "") else None
    if status in NOT_OK and not dettaglio:
        dettaglio = "nessun dettaglio fornito"
    entry.update({
        "stato": status,
        "finished_at": finished_at,
        "durata": duration,
        "error": dettaglio if status in NOT_OK else None,
        "note": None,
    })
    d["runs"] = runs
    if str(d.get("run_seq")) == str(run_id):
        d["last_status"] = status if status not in NOT_OK else f"{status}: {dettaglio}"
    d["updated_at"] = finished_at
    _write(d)
    return True


def count_fire(job_id: int) -> Optional[dict]:
    """Registra UNA ripetizione consumata e, se erano le ultime, disabilita il
    job. Ritorna il record aggiornato (con `fired_count` e `enabled` correnti).

    Chiamata solo dai fire effettivamente DISPATCHATI: uno skip-if-busy non ha
    postato niente nel topic, quindi non ha speso una delle M ripetizioni
    chieste dall'owner (#239). `repeat_count = 0` = senza fine: conta comunque,
    così il pannello mostra quante volte è partito, ma non disabilita mai."""
    d = get_job(job_id)
    if d is None:
        return None
    d["fired_count"] = _non_neg_int(d.get("fired_count")) + 1
    limite = _non_neg_int(d.get("repeat_count"))
    if limite and d["fired_count"] >= limite:
        d["enabled"] = False
    d["updated_at"] = _now_iso()
    _write(d)
    return d


def iter_enabled_jobs() -> Iterable[dict]:
    """Itera sui job enabled (per il bootstrap dello scheduler)."""
    for j in _all():
        if j.get("enabled"):
            yield j
