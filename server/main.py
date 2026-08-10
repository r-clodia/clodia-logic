import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

# ANTHROPIC_API_KEY vuota (es. da docker-compose) blocca il login OAuth:
# il CLI la vede e tenta la modalità API key ignorando ~/.claude/.
# La rimuoviamo al boot se vuota — utenti con API key reale non sono impattati.
if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ.pop("ANTHROPIC_API_KEY", None)

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, instance_profile
from .api import admin, agent_registry, agents, auth, catalog, channel_aliases, channels, connectors, files, gate, health, human_auth, observe, packs, plugins, profile, providers, spawns, topics, transfers
from .config import HOST, PORT
from .scheduler import api as jobs_api
from .scheduler import (
    init_db as scheduler_init_db,
    reload_all_enabled_jobs,
    shutdown_scheduler,
    start_scheduler,
)
from .sdk_runtime.session import manager

LOG = logging.getLogger("agent-server")

# Assicura un handler sul root logger così che i messaggi `LOG.info(...)` di
# 'agent-server' e 'scheduler' finiscano nello stdout sotto uvicorn (che
# configura solo i propri logger 'uvicorn.*').
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

# Sicurezza (Prima Legge): a livello INFO httpx logga l'URL completo di ogni
# richiesta, e le chiamate Trello portano `key` e `token` come query param →
# le credenziali finirebbero in chiaro nei log del container. Alziamo a
# WARNING i logger di httpx/httpcore così le request-line con i segreti non
# vengono più emesse (restano solo gli errori).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Log su FILE nella datadir (oltre a stdout): serve al tool `logs.tail` di
# sysadmin, che legge il file dal datadir condiviso col gateway. Rotante per
# non crescere illimitato. I segreti sono già soppressi sopra (httpx→WARNING).
try:
    from logging.handlers import RotatingFileHandler
    from .config import data_path
    _logdir = data_path("logs")
    _logdir.mkdir(parents=True, exist_ok=True)
    _fh = RotatingFileHandler(_logdir / "agent-server.log",
                              maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception as _e:  # noqa: BLE001 — il file di log non deve mai bloccare il boot
    logging.getLogger("agent-server").warning("file log non attivo: %s", _e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # --- startup ---
    # Sweep degli spawn ORFANI: al boot nessuna sessione è viva, quindi tutte le
    # cartelle spawn rimaste da prima del restart/crash sono garbage (le sessioni
    # ne materializzano di nuove on-demand, non riusano le vecchie). Le rimuove
    # PRIMA di ricreare la chat di default, così non tocca uno spawn fresco.
    try:
        from .agents.workspace import sweep_orphan_spawns
        n = await asyncio.to_thread(sweep_orphan_spawns, set(), 0.0)
        if n:
            LOG.info("boot: rimossi %d spawn orfani dalla datadir", len(n))
    except Exception as e:  # noqa: BLE001 — non deve mai impedire l'avvio
        LOG.warning("sweep spawn orfani al boot fallito: %s", e)
    # Bootstrap: se l'istanza non è reclamata, logga il bootstrap token (serve
    # all'owner per l'enroll del primo admin) e NON creare la chat di default.
    # I SEED del base-pack arrivano PRIMA di tutto il resto: uno spawn è fatto da
    # un seed, quindi materializzarli dopo significherebbe poter nascere senza.
    # Le altre tre sync (skill, rule, costituzioni) girano alla materializzazione
    # di uno spawn; questa no, e la differenza è quella.
    # IL CONFINE, verificato da dentro (invariante 8). Non poggia su codice
    # nostro ma sui permessi delle directory, cioè su come l'istanza è montata:
    # va misurato su OGNI istanza a ogni avvio, non dedotto una volta da un
    # mount letto altrove. Se cade, cade in silenzio — ed è per questo che il
    # controllo grida.
    try:
        from .agents.boundary_check import check as _boundary
        _boundary()
    except Exception as e:  # noqa: BLE001 — una verifica che fallisce non blocca
        LOG.warning("verifica del confine non eseguita: %s", e)
    try:
        from .agents.seed_sync import sync_seeds
        nuovi = sync_seeds()
        if nuovi:
            LOG.info("seed materializzati dal base-pack: %s", ", ".join(nuovi))
    except Exception as e:  # noqa: BLE001 — un seed mancante non blocca il boot
        LOG.warning("sync dei seed dal pack fallita: %s", e)
    try:
        if not admin.is_initialized():
            LOG.warning("[BOOTSTRAP] istanza NON reclamata — apri la webui e crea il "
                        "primo admin (popup Nuovo agente, tipo human). Nessuna altra "
                        "azione è permessa finché non c'è un superadmin.")
    except Exception as e:  # noqa: BLE001
        LOG.warning("bootstrap check fallito: %s", e)
    # Pack di skill esterni (anthropic-pack, openai-curated-pack…): installati
    # al setup iniziale, in background (clone di rete) e idempotenti. Non devono
    # mai bloccare/ritardare il boot né farlo fallire.
    async def _safe_install_packs():
        try:
            from .setup.external_packs import install_external_packs
            res = await asyncio.to_thread(
                install_external_packs, False, profile.skill_packs)
            if res:
                LOG.info("Pack esterni installati: %s", res)
        except Exception as e:  # noqa: BLE001
            LOG.warning("install pack esterni fallito: %s", e)
    asyncio.create_task(_safe_install_packs())

    profile = instance_profile.load()

    # Scheduler: init DB, start, riconcilia jobstore con jobs table.
    # Feature `jobs` (profilo istanza): se spenta, lo scheduler non parte.
    if profile.features.jobs:
        try:
            scheduler_init_db()
            loop = asyncio.get_running_loop()
            start_scheduler(loop)
            n = reload_all_enabled_jobs()
            LOG.info("Scheduler pronto: %d job enabled caricati", n)
        except Exception as e:
            # Lo scheduler non deve mai impedire l'avvio del server. Loggiamo e
            # proseguiamo — le API /clodia/jobs risponderanno con errori se
            # invocate, e l'operatore vedrà il problema nei log.
            LOG.exception("Errore di avvio dello scheduler: %s", e)
    else:
        LOG.info("feature 'jobs' OFF (profilo '%s'): scheduler non avviato", profile.edition)

    # Pack ops (Sysadmin): riconciliazione a stato desiderato al BOOT, se i
    # plugin installati dichiarano requires:/datastores:. Chiude il problema
    # del filesystem effimero: dopo un recreate del container l'agente
    # riconverge (install già in path persistenti → no-op veloce). Best-effort,
    # ritardata per lasciare al runtime il tempo di collegare i provider;
    # senza dichiarazioni non parte nessuna sessione (zero costo).
    async def _safe_boot_reconcile():
        try:
            await asyncio.sleep(30)
            from .api import pack_ops
            res = await pack_ops.trigger_reconcile("boot")
            if res.get("triggered"):
                LOG.info("pack ops: boot reconcile consegnato a '%s'", res.get("agent"))
        except Exception as e:  # noqa: BLE001
            LOG.warning("pack ops boot reconcile fallito: %s", e)
    asyncio.create_task(_safe_boot_reconcile())

    # Relay inbound Telegram → topic (modello telegram-proxy): LONG-POLL server-side,
    # trasporto MECCANICO (nessuna logica AI). Ogni ciclo blocca (in un thread) su
    # getUpdates finché arriva un messaggio → latenza quasi zero, meno carico dei
    # poll ripetuti. Instrada le chat legate ai topic e innesca il responder solo se
    # il bot è interpellato. Gattato da `channels`. Non impedisce mai l'avvio.
    async def _channel_relay_loop():
        from .api import channel_relay
        timeout = int(os.environ.get("CLODIA_CHANNEL_POLL_TIMEOUT", "25"))
        while True:
            try:
                await channel_relay.run_poll_cycle(timeout)
            except Exception as e:  # noqa: BLE001
                LOG.warning("channel relay poll: %s", e)
                await asyncio.sleep(2)   # backoff su errore (es. gateway irraggiungibile)
    if profile.features.channels:
        relay_task = asyncio.create_task(_channel_relay_loop())
    else:
        LOG.info("feature 'channels' OFF (profilo '%s'): relay telegram non avviato",
                 profile.edition)
        relay_task = asyncio.create_task(asyncio.sleep(0))  # no-op cancellabile

    # Idle reaper: evince periodicamente le sessioni chat idle (subprocess
    # claude/codex ancora vivo + spawn su disco) per recuperare RAM e disco.
    # Senza, le sessioni lasciate aperte si accumulano fino a saturare la
    # memoria (swap → healthcheck in timeout → container unhealthy).
    # Configurabile: TTL idle e cadenza tick via env; TTL<=0 disabilita.
    async def _idle_reaper_loop():
        ttl = float(os.environ.get("CLODIA_SESSION_IDLE_TTL_SEC", "1800"))       # 30 min
        hard_ttl = float(os.environ.get("CLODIA_SESSION_HARD_TTL_SEC", "3600"))
        interval = float(os.environ.get("CLODIA_SESSION_REAP_TICK_SEC", "300"))  # 5 min
        if ttl <= 0:
            LOG.info("idle reaper disabilitato (CLODIA_SESSION_IDLE_TTL_SEC<=0)")
            return
        LOG.info("idle reaper attivo: ttl=%.0fs hard_ttl=%.0fs tick=%.0fs",
                 ttl, hard_ttl, interval)
        from .agents.workspace import sweep_orphan_spawns
        from .sdk_runtime.process_reaper import sweep_orphan_runtime_processes
        while True:
            await asyncio.sleep(interval)
            try:
                await manager.reap_idle(ttl)
            except Exception as e:  # noqa: BLE001
                LOG.warning("idle reaper tick: %s", e)
            # Sweep degli spawn orfani a runtime (crash/sessione evinta senza
            # cleanup): rimuove solo dir NON di sessioni vive e vecchie almeno
            # `ttl` (protegge spawn recenti/di job non tracciati dal manager).
            live = manager.live_spawn_dirs()
            try:
                await asyncio.to_thread(sweep_orphan_spawns, live, ttl)
            except Exception as e:  # noqa: BLE001
                LOG.warning("sweep spawn orfani (reaper): %s", e)
            # Fallback sul process table: recupera subprocess Claude sfuggiti
            # al registry dopo crash o stop incompleto. La policy è volutamente
            # stretta (discendenti del server + cwd non appartenente a chat vive).
            if hard_ttl > 0:
                try:
                    stats = await asyncio.to_thread(
                        sweep_orphan_runtime_processes, live, hard_ttl
                    )
                    if stats["reaped"]:
                        LOG.warning("runtime reaper: terminati %d processi orfani",
                                    stats["reaped"])
                except Exception as e:  # noqa: BLE001
                    LOG.warning("sweep processi runtime orfani: %s", e)
    reaper_task = asyncio.create_task(_idle_reaper_loop())

    yield
    # --- shutdown ---
    relay_task.cancel()
    reaper_task.cancel()
    try:
        shutdown_scheduler()
    except Exception as e:  # pragma: no cover
        LOG.warning("Errore in shutdown_scheduler: %s", e)


def create_app() -> FastAPI:
    app = FastAPI(title="Clodia agent-server", version=__version__, lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Gate di bootstrap (F1): finché l'istanza non è reclamata (nessun
    # superadmin) NON si rivela nulla — è consentito SOLO lo stretto necessario
    # al setup: leggere lo stato admin, creare il primo human (→ superadmin) e
    # l'infra (health/docs). Ogni altra richiesta (incluse le letture di
    # agents/chats/topics/skills/providers) → 423. (F2 aggiungerà la verifica
    # del session token quando inizializzata.)
    def _preclaim_allowed(method: str, path: str) -> bool:
        if method in ("HEAD", "OPTIONS"):
            return True  # preflight/innocui
        if method == "GET":
            return (path.startswith("/api/admin")
                    or path == "/health"
                    # Profilo dell'edizione: solo features+branding, nessun
                    # segreto — serve PRE-claim perché la schermata di claim/
                    # login di un'edizione custom mostri il suo branding e la
                    # webui non ripieghi su FULL durante il bootstrap.
                    or path == "/profile"
                    or path == "/profile/logo"
                    or path == "/openapi.json"
                    or path.startswith("/docs")
                    or path.startswith("/redoc"))
        if method == "POST":
            return path == "/api/agents"  # SOLO la creazione del primo superadmin
        return False

    @app.middleware("http")
    async def _bootstrap_gate(request, call_next):
        if admin.is_initialized() or _preclaim_allowed(request.method, request.url.path):
            return await call_next(request)
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"error": "uninitialized",
             "detail": "istanza non reclamata: configura il superadmin"},
            status_code=423)

    # Profilo d'istanza (Modular Distro F1): feature spenta = router NON
    # montato (endpoint inesistente, 404) — riduzione reale della superficie.
    # Profilo assente = FULL → comportamento identico a prima.
    prof = instance_profile.load()

    # Un gateway irraggiungibile non è un 500. Trovato dal vivo: durante il
    # riavvio del gateway un endpoint che non catturava `TopicsClientError` ha
    # fatto uscire l'eccezione, e la UI ha mostrato «HTTP 500 — Internal Server
    # Error», cioè il messaggio che non dice niente. I punti che lo catturano sono
    # decine e ne basta uno che dimentica: l'handler globale è l'unico modo per
    # cui non può essere dimenticato.
    #
    # 503 e non 502: il gateway non ha risposto male, non ha risposto — ed è una
    # condizione TRANSITORIA, che è precisamente l'informazione utile (riprova).
    from .api.topics_client import TopicsClientError as _TCErr

    @app.exception_handler(_TCErr)
    async def _gateway_error(_request, exc: _TCErr):  # noqa: ANN202
        from fastapi.responses import JSONResponse as _JR
        if exc.is_client_error:
            return _JR({"detail": exc.detail}, status_code=exc.status or 400)
        unreachable = "irraggiungibile" in str(exc)
        LOG.warning("gateway: %s", str(exc)[:200])
        return _JR({"detail": (
            "gateway dei topic non raggiungibile — probabilmente si sta "
            "riavviando. Riprova fra qualche secondo." if unreachable
            else f"errore dal gateway dei topic: {exc.detail[:200]}")},
            status_code=503 if unreachable else 502)

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(auth.router)
    app.include_router(human_auth.router)
    app.include_router(agents.router)
    app.include_router(channels.router)
    app.include_router(gate.router)
    app.include_router(observe.router)
    app.include_router(connectors.router)
    app.include_router(agent_registry.router)
    app.include_router(files.router)
    if prof.features.topics != "off":
        app.include_router(topics.router)
        # Segnali per-principal dei recent topics: badge azionabile + pallino
        # attività (issue clodia-platform#83).
        from .api import topic_signals
        app.include_router(topic_signals.router)
        # Chat Hooks: iniezione di messaggi in una chat via hook/webhook (F1).
        from .hooks import api as hooks_api
        app.include_router(hooks_api.router)
    # Pagina di decisione dei gate via link firmato (no login, token-auth):
    # deve essere raggiungibile senza sessione (arrivi da mail/Telegram).
    # Montata SEMPRE: serve le proposte di job, ed era dietro il flag dei
    # workflow solo perché una volta serviva anche quelli. Un link che arriva
    # per mail e trova 404 perché una feature spenta non c'entra nulla con lui
    # è indistinguibile da un link scaduto.
    from .api import gate_public
    app.include_router(gate_public.router)
    app.include_router(profile.router)
    app.include_router(channel_aliases.router)
    app.include_router(catalog.router)
    app.include_router(packs.router)
    app.include_router(plugins.router)
    app.include_router(providers.router)
    app.include_router(spawns.router)
    app.include_router(transfers.router)
    if prof.features.jobs:
        app.include_router(jobs_api.router)

    @app.get("/profile")
    async def get_instance_profile() -> dict:
        """Profilo pubblico dell'istanza per la webui (nessun segreto)."""
        return instance_profile.public_view()

    @app.get("/profile/logo")
    async def get_instance_logo():
        """Il logo dell'istanza, dalla datadir. **Senza autenticazione**, di
        proposito: deve comparire sulla schermata di accesso, cioè prima che
        esista una sessione — è lì che un cliente vede per la prima volta il
        proprio marchio invece del nostro.

        Non è una porta di servizio sui file: il path lo sceglie l'ADMIN nel
        profilo, non il chiamante, e viene confinato dentro la datadir. Senza
        quel confinamento un profilo con `logo: ../../etc/passwd` diventerebbe
        una lettura arbitraria servita pubblicamente — il tipo di difetto che
        nasce quando un valore di configurazione viene trattato come fidato solo
        perché lo scrive un admin.
        """
        from fastapi.responses import FileResponse
        from .config import data_path
        rel = (instance_profile.load().branding.logo or "").strip()
        if not rel:
            raise HTTPException(404, "nessun logo configurato")
        base = data_path("").resolve()
        try:
            p = (base / rel).resolve()
            p.relative_to(base)
        except (ValueError, OSError):
            raise HTTPException(400, "branding.logo esce dalla datadir")
        if not p.is_file():
            raise HTTPException(404, f"logo non trovato: {rel}")
        import mimetypes as _mt
        tipo = _mt.guess_type(str(p))[0] or "application/octet-stream"
        if not tipo.startswith("image/"):
            raise HTTPException(400, f"branding.logo non è un'immagine ({tipo})")
        return FileResponse(p, media_type=tipo,
                            headers={"Cache-Control": "public, max-age=300"})

    @app.patch("/profile")
    async def patch_instance_profile(request: Request) -> dict:
        """Patch admin-only del profilo runtime. Per ora espone solo channel_aliases."""
        from .api.agents import _principal_from_request
        principal = _principal_from_request(request)
        if not admin.is_admin(principal):
            raise HTTPException(403, "solo un admin può modificare il profilo")
        body = await request.json()
        allowed = {"channel_aliases"}
        unknown = set(body) - allowed
        if unknown:
            raise HTTPException(400, f"campi non modificabili: {', '.join(sorted(unknown))}")
        try:
            return instance_profile.update_channel_aliases(body.get("channel_aliases") or {})
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/profile/logo")
    async def get_instance_logo():
        """Logo dell'edizione (branding.logo, path relativo alla datadir).
        Pre-claim: il marchio del cliente appare già sulla schermata di login.
        Path-safety: il file risolto deve stare DENTRO la datadir."""
        from fastapi.responses import FileResponse, JSONResponse as _JR
        from .config import CLODIA_DATA
        rel = instance_profile.load().branding.logo
        if not rel:
            return _JR(status_code=404, content={"error": "nessun logo configurato"})
        path = (CLODIA_DATA / rel).resolve()
        if CLODIA_DATA.resolve() not in path.parents or not path.is_file():
            return _JR(status_code=404, content={"error": "logo non trovato"})
        return FileResponse(path)

    # Nessun frontend embedded: la webui ufficiale è clodia-web (servita a
    # parte). L'agent-server espone solo le API REST.
    return app


app = create_app()


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    run()
