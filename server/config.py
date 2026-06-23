import os
from pathlib import Path
import yaml

# Layout repo (refactor v4): il pacchetto `server/` vive alla root del repo
# clodia-logic. `server/config.py` → parent = server/, parent.parent = repo root.
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent  # root del repo logic
# Backcompat: alcuni call site usavano TOOL_ROOT (la vecchia cartella
# tools/system/agent-server). Dopo l'appiattimento coincide con la root.
TOOL_ROOT = WORKSPACE_ROOT
# CLODIA_DATA separa i dati d'istanza (topics, secrets, providers) dalla logica.
# In Docker: CLODIA_DATA=/datadir; in locale fallback a WORKSPACE_ROOT.
CLODIA_DATA = Path(os.environ.get("CLODIA_DATA", str(WORKSPACE_ROOT)))


def _load_config() -> dict:
    cfg_path = WORKSPACE_ROOT / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


CONFIG = _load_config()


def workspace_path(rel: str) -> Path:
    """Resolve a path relative to the Clodia bundle root (logic/tools)."""
    return WORKSPACE_ROOT / rel


def data_path(rel: str) -> Path:
    """Resolve a path relative to the Clodia data directory (instance data)."""
    return CLODIA_DATA / rel


HOST = os.environ.get("SERVER_HOST", CONFIG["server"]["host"])
PORT = CONFIG["server"]["port"]
LOG_LEVEL = CONFIG["server"]["log_level"]

DEFAULT_MODEL = CONFIG["sdk"]["default_model"]
SESSION_IDLE_TIMEOUT = CONFIG["sdk"]["session_idle_timeout_seconds"]

LOGS_DIR = WORKSPACE_ROOT / CONFIG["logging"]["dir"]
LOGS_DIR.mkdir(exist_ok=True)
