"""Import di skill UTENTE da archivio .zip o da URL (git repo o .zip remoto).

Le skill importate finiscono tutte nel pack `user-pack`, storage pack-subdir
`CLODIA_DATA/skills-catalog/user-pack/<skill>/`. Niente editing locale: una
skill è un asset che si importa/rimuove, non si scrive a mano.

Sicurezza (Prima Legge): estrazione zip protetta da zip-slip + limiti di
dimensione/conteggio; URL solo http(s); git clone shallow con timeout.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import catalog

LOG = logging.getLogger("agent-server.api.skill_import")

_MAX_ZIP_BYTES = 50 * 1024 * 1024       # 50 MB compressi
_MAX_UNCOMPRESSED = 200 * 1024 * 1024   # 200 MB estratti
_MAX_FILES = 5000
_CLONE_TIMEOUT = 180
_DOWNLOAD_TIMEOUT = 120


class SkillImportError(Exception):
    """Errore d'import gestito (→ 400 lato API)."""


def _safe_extract_zip(data: bytes, dest: Path) -> None:
    if len(data) > _MAX_ZIP_BYTES:
        raise SkillImportError("archivio troppo grande (max 50MB)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillImportError("file .zip non valido")
    infos = zf.infolist()
    if len(infos) > _MAX_FILES:
        raise SkillImportError("archivio con troppi file")
    total = sum(i.file_size for i in infos)
    if total > _MAX_UNCOMPRESSED:
        raise SkillImportError("contenuto estratto troppo grande")
    dest_resolved = dest.resolve()
    for info in infos:
        # zip-slip guard: ogni membro deve restare dentro dest
        target = (dest / info.filename).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise SkillImportError(f"percorso non sicuro nell'archivio: {info.filename}")
    zf.extractall(dest)


def _git_clone(url: str, dest: Path) -> None:
    try:
        subprocess.run(["git", "clone", "--depth", "1", url, str(dest)],
                       check=True, capture_output=True, timeout=_CLONE_TIMEOUT)
    except subprocess.CalledProcessError as e:
        raise SkillImportError(f"git clone fallito: {e.stderr.decode()[:160]}")
    except subprocess.TimeoutExpired:
        raise SkillImportError("git clone in timeout")


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "clodia-skill-import"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as r:  # noqa: S310 (http(s) validato)
        return r.read(_MAX_ZIP_BYTES + 1)


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Cartelle che contengono un SKILL.md. Se la root stessa è una skill, è
    l'unica; altrimenti tutte quelle trovate (supporta zip multi-skill)."""
    if (root / "SKILL.md").is_file():
        return [root]
    dirs = sorted({sm.parent for sm in root.rglob("SKILL.md") if ".git" not in sm.parts})
    return dirs


def _install_skill_dir(sdir: Path) -> str:
    """Copia la cartella skill in `skills-catalog/user-pack/<name>/`. Il nome
    viene dal frontmatter `name`, con fallback al nome cartella. Ritorna il nome."""
    try:
        frontmatter, _b, _f = catalog._read_catalog_file(sdir / "SKILL.md")
    except Exception:
        frontmatter = {}
    name = str(frontmatter.get("name") or sdir.name).strip()
    if not catalog._NAME_RE.fullmatch(name):
        raise SkillImportError(
            f"nome skill non valido: '{name}' (minuscole, cifre, - e _)")
    if name in catalog._iter_skill_paths(catalog.LOGIC_SKILLS_DIR):
        raise SkillImportError(f"'{name}' è una skill nativa (base-pack), non importabile")
    dst = catalog.DATA_SKILLS_DIR / catalog.USER_PACK / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(sdir, dst, ignore=shutil.ignore_patterns(".git"))
    return name


def _install_from_root(root: Path) -> list[str]:
    skill_dirs = _discover_skill_dirs(root)
    if not skill_dirs:
        raise SkillImportError("nessun SKILL.md trovato nell'archivio/URL")
    installed = [_install_skill_dir(d) for d in skill_dirs]
    catalog._invalidate("skill")
    return installed


def import_zip(data: bytes) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="clodia-skill-zip-") as tmp:
        root = Path(tmp)
        _safe_extract_zip(data, root)
        return _install_from_root(root)


def import_url(url: str) -> list[str]:
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise SkillImportError("URL non valido (richiesto http/https)")
    with tempfile.TemporaryDirectory(prefix="clodia-skill-url-") as tmp:
        root = Path(tmp)
        if url.lower().endswith(".zip"):
            data = _download(url)
            _safe_extract_zip(data, root)
        else:
            _git_clone(url, root)
        return _install_from_root(root)
