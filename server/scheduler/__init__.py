"""Cron scheduler per spawn di chat Looper effimere.

Sostituisce il modello "Looper sempre vivo" con uno scheduler daemon che, al
fire di un cron, materializza uno spawn dell'executor con un prompt predefinito.
Persistenza dei job in file-per-job (jobs/<id>.yaml, editabili e clonabili);
isolamento per esecuzione (ogni fire = spawn indipendente); niente LLM in idle.
"""
from .db import (
    init_db,
    create_job,
    get_job,
    list_jobs,
    update_job,
    delete_job,
    mark_run,
    JOBS_DIR,
)
from .scheduler import (
    start_scheduler,
    shutdown_scheduler,
    register_job,
    unregister_job,
    fire_job,
    reload_all_enabled_jobs,
    validate_cron_expr,
)

__all__ = [
    "init_db",
    "create_job",
    "get_job",
    "list_jobs",
    "update_job",
    "delete_job",
    "mark_run",
    "JOBS_DIR",
    "start_scheduler",
    "shutdown_scheduler",
    "register_job",
    "unregister_job",
    "fire_job",
    "reload_all_enabled_jobs",
    "validate_cron_expr",
]
