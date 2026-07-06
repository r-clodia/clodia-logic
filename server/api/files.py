"""File download + upload endpoint con whitelist di root.

- Download: serve file dal filesystem locale ai client della GUI quando
  Clodia mostra un path nella chat. Validazione server-side: path assoluto,
  risolto (no symlink out-of-bounds), dentro ALLOWED_ROOTS, esclude path
  con segmenti sensibili (secrets/, .ssh, ecc.).

- Upload: riceve file dropped dalla GUI e li scrive in dump/ (porta di
  scambio Clodia↔owner). Nome sanitizzato, anti-collisione con suffisso
  numerico, limite 100 MB per file.
"""
import re
import unicodedata
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from ..config import CLODIA_DATA, WORKSPACE_ROOT

router = APIRouter()

# dump/ è il punto di scambio Clodia↔owner (vale su Mac, Docker e Linux)
DUMP_DIR = WORKSPACE_ROOT / "dump"
DUMP_DIR.mkdir(exist_ok=True)

# Limite di dimensione per upload (100 MB)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Root da cui è permesso servire file (dinamico: funziona su Mac e in Docker)
ALLOWED_ROOTS = [
    WORKSPACE_ROOT.resolve(),
    CLODIA_DATA.resolve(),
    Path("/tmp").resolve(),
]

# Segmenti di path che blockiamo anche se dentro ALLOWED_ROOTS
DENIED_SEGMENTS = {
    "secrets",
    ".ssh",
    ".aws",
    ".gnupg",
    ".claude",         # contiene credentials Anthropic, config personali
}


def _is_allowed(p: Path) -> bool:
    """True se p è dentro almeno una root permessa e non contiene segmenti vietati."""
    try:
        resolved = p.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return False
    # Deve essere file (non directory, non special)
    if not resolved.is_file():
        return False
    # Almeno una root deve contenerlo
    in_root = False
    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root)
            in_root = True
            break
        except ValueError:
            continue
    if not in_root:
        return False
    # Nessun segmento vietato
    for part in resolved.parts:
        if part in DENIED_SEGMENTS:
            return False
    return True


def _require_login(request) -> str:
    """Fix sicurezza 7 lug 2026: gli endpoint /files erano senza auth —
    chiunque col link scaricava file della datadir. Ora serve una sessione."""
    from .agents import _principal_from_request
    principal = _principal_from_request(request)
    if not principal:
        raise HTTPException(401, "login richiesto")
    return principal


@router.get("/files/download")
async def download_file(request: Request, path: str = Query(..., description="Absolute path to the file")):
    _require_login(request)
    if not path.startswith("/"):
        raise HTTPException(400, "path must be absolute")
    p = Path(path)
    if not _is_allowed(p):
        raise HTTPException(403, "path not allowed")
    return FileResponse(
        path=str(p.resolve()),
        filename=p.name,
        media_type="application/octet-stream",
    )


@router.get("/files/check")
async def check_file(request: Request, path: str = Query(...)):
    _require_login(request)
    """Endpoint leggero per il frontend: dice se un path è scaricabile senza fare il download.
    Restituisce {allowed: bool, name: str, size: int|null}."""
    out = {"allowed": False, "name": "", "size": None}
    if not path.startswith("/"):
        return out
    p = Path(path)
    if not _is_allowed(p):
        return out
    resolved = p.resolve()
    out["allowed"] = True
    out["name"] = resolved.name
    try:
        out["size"] = resolved.stat().st_size
    except OSError:
        pass
    return out


def _sanitize_filename(name: str) -> str:
    """Estrae un nome file sicuro: solo basename, normalizzato, niente segmenti
    di path o caratteri di controllo. Se vuoto dopo sanitizzazione → 'upload'."""
    # Solo basename (anti path-traversal)
    base = Path(name).name
    # Normalizza unicode
    base = unicodedata.normalize("NFKC", base)
    # Sostituisce ogni char non sicuro (mantiene lettere, cifre, ._-, spazio)
    base = re.sub(r"[^\w.\- ]", "_", base, flags=re.UNICODE)
    # Collassa spazi multipli, strip
    base = re.sub(r"\s+", " ", base).strip(" .")
    # Limita lunghezza (filesystem-safe)
    if len(base) > 200:
        stem = Path(base).stem[:180]
        suffix = Path(base).suffix[:20]
        base = stem + suffix
    return base or "upload"


def _unique_destination(directory: Path, filename: str) -> Path:
    """Restituisce un path in `directory` non collidente: se esiste, aggiunge
    suffisso -1, -2, … prima dell'estensione."""
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


@router.post("/files/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Riceve un file droppato dalla GUI e lo salva in dump/.

    Validazioni:
    - Nome sanitizzato (no path traversal, no caratteri pericolosi)
    - Anti-collisione con suffisso numerico (-1, -2, ...)
    - Dimensione max MAX_UPLOAD_BYTES (default 100 MB)

    Risposta JSON: {ok, path, name, size}.
    """
    _require_login(request)
    if not DUMP_DIR.is_dir():
        raise HTTPException(500, f"upload dir {DUMP_DIR} non esiste")

    original = file.filename or "upload"
    safe_name = _sanitize_filename(original)
    dest = _unique_destination(DUMP_DIR, safe_name)

    # Scrittura streaming a chunk con enforcement del limite di size
    written = 0
    chunk_size = 1024 * 1024  # 1 MB
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    out.close()
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"file troppo grande (>{MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise HTTPException(500, f"errore scrittura file: {exc}") from exc

    return {
        "ok": True,
        "path": str(dest),
        "name": dest.name,
        "size": written,
    }
