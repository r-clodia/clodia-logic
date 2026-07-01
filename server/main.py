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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import admin, agent_registry, agents, auth, catalog, channels, connectors, files, health, human_auth, profile, providers, spawns, topics
from .config import HOST, PORT
from .scheduler import api as jobs_api
from .scheduler import (
    init_db as scheduler_init_db,
    reload_all_enabled_jobs,
    shutdown_scheduler,
    start_scheduler,
)
from .sdk_runtime.session import manager, DEFAULT_CHAT_ID

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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # --- startup ---
    # Bootstrap: se l'istanza non è reclamata, logga il bootstrap token (serve
    # all'owner per l'enroll del primo admin) e NON creare la chat di default.
    try:
        if not admin.is_initialized():
            LOG.warning("[BOOTSTRAP] istanza NON reclamata — apri la webui e crea il "
                        "primo admin (popup Nuovo agente, tipo human). Nessuna altra "
                        "azione è permessa finché non c'è un superadmin.")
        else:
            asyncio.create_task(_safe_create_default())
    except Exception as e:  # noqa: BLE001
        LOG.warning("bootstrap check fallito: %s", e)
        asyncio.create_task(_safe_create_default())
    # Pack di skill esterni (anthropic-pack, openai-curated-pack…): installati
    # al setup iniziale, in background (clone di rete) e idempotenti. Non devono
    # mai bloccare/ritardare il boot né farlo fallire.
    async def _safe_install_packs():
        try:
            from .setup.external_packs import install_external_packs
            res = await asyncio.to_thread(install_external_packs)
            if res:
                LOG.info("Pack esterni installati: %s", res)
        except Exception as e:  # noqa: BLE001
            LOG.warning("install pack esterni fallito: %s", e)
    asyncio.create_task(_safe_install_packs())

    # Scheduler: init DB, start, riconcilia jobstore con jobs table.
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
    # Channel-adapter Telegram: loop periodico server-side (trasporto in codice,
    # nessuna logica AI). Non deve mai impedire l'avvio: gira in background.
    async def _channel_adapter_loop():
        from .api import channel_adapter
        interval = int(os.environ.get("CLODIA_CHANNEL_TICK_SEC", "45"))
        while True:
            try:
                await channel_adapter.tick_once()
            except Exception as e:  # noqa: BLE001
                LOG.warning("channel adapter tick: %s", e)
            await asyncio.sleep(interval)
    channel_task = asyncio.create_task(_channel_adapter_loop())

    # Idle reaper: evince periodicamente le sessioni chat idle (subprocess
    # claude/codex ancora vivo + spawn su disco) per recuperare RAM e disco.
    # Senza, le sessioni lasciate aperte si accumulano fino a saturare la
    # memoria (swap → healthcheck in timeout → container unhealthy).
    # Configurabile: TTL idle e cadenza tick via env; TTL<=0 disabilita.
    async def _idle_reaper_loop():
        ttl = float(os.environ.get("CLODIA_SESSION_IDLE_TTL_SEC", "1800"))       # 30 min
        interval = float(os.environ.get("CLODIA_SESSION_REAP_TICK_SEC", "300"))  # 5 min
        if ttl <= 0:
            LOG.info("idle reaper disabilitato (CLODIA_SESSION_IDLE_TTL_SEC<=0)")
            return
        LOG.info("idle reaper attivo: ttl=%.0fs tick=%.0fs", ttl, interval)
        while True:
            await asyncio.sleep(interval)
            try:
                await manager.reap_idle(ttl, protect=frozenset({DEFAULT_CHAT_ID}))
            except Exception as e:  # noqa: BLE001
                LOG.warning("idle reaper tick: %s", e)
    reaper_task = asyncio.create_task(_idle_reaper_loop())

    yield
    # --- shutdown ---
    channel_task.cancel()
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

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(auth.router)
    app.include_router(human_auth.router)
    app.include_router(agents.router)
    app.include_router(channels.router)
    app.include_router(connectors.router)
    app.include_router(agent_registry.router)
    app.include_router(files.router)
    app.include_router(topics.router)
    app.include_router(profile.router)
    app.include_router(catalog.router)
    app.include_router(providers.router)
    app.include_router(spawns.router)
    app.include_router(jobs_api.router)

    # Nessun frontend embedded: la webui ufficiale è clodia-web (servita a
    # parte). L'agent-server espone solo le API REST.
    return app


async def _safe_create_default() -> None:
    try:
        await manager.create(chat_id=DEFAULT_CHAT_ID)
    except ValueError:
        pass  # già esistente
    except Exception:
        pass


app = create_app()


def run() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    run()
