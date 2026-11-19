"""Verifica DA DENTRO che uno spawn non raggiunga ciò che non deve.

Invariante 8 della specifica, e fino all'8 ago 2026 era **scritta e non asserita
da nulla** — proprio quella che più ne aveva bisogno, perché non poggia su codice
nostro ma su come l'istanza è montata.

**L'invariante era anche formulata male.** Diceva «l'agent-server non vede il
topic store», misurato una volta sullo stack personale dove una maschera di
compose lo nasconde. Su venere quella riga non c'è: il processo agent-server gira
come root e il vault lo vede. Un test scritto su quella formulazione sarebbe
diventato rosso mandando a inseguire una differenza di configurazione invece di
una proprietà di sicurezza.

La proprietà che conta è un'altra e vale su entrambe: **uno SPAWN non raggiunge
il vault, il topic store, né i seed**. Non perché una riga di compose lo nasconda
al processo, ma perché quelle directory sono `drwx------ root` e gli spawn girano
unprivileged. Il confine lo mette il **kernel**, che è il solo tipo di confine che
non si perde in un aggiornamento del compose.

Gira al boot e **logga forte** se cade: una protezione che dipende da come
l'istanza è montata va verificata su ogni istanza, non dedotta da un mount letto
una volta.
"""
from __future__ import annotations

import logging
import os
import stat
import sys

LOG = logging.getLogger("agent-server.agents.boundary")

#: uid/gid con cui girano gli spawn. Se cambiassero senza aggiornare qui, questo
#: controllo misurerebbe un utente che non esiste e passerebbe per la ragione
#: sbagliata — quindi il valore reale viene letto dal filesystem quando c'è.
SPAWN_UID = 60000
SPAWN_GID = 62554

#: Ciò che uno spawn non deve raggiungere, e perché.
VIETATI = {
    "/datadir/clodia-vault": "il vault: credenziali e token di ogni integrazione",
    "/datadir/clodia-vault/topics-store": "i dati di OGNI topic, non solo dei propri",
    "/datadir/agents": "i seed, dove vivono ruoli e matrici — l'autorità stessa",
}


def _spawn_uid() -> int:
    """L'uid VERO di uno spawn, letto da una directory esistente. Il default è
    un ripiego: verificare il confine per un utente che non esiste passerebbe
    per la ragione sbagliata."""
    root = "/datadir/spawns"
    try:
        for n in sorted(os.listdir(root)):
            p = os.path.join(root, n)
            if os.path.isdir(p):
                return os.stat(p).st_uid
    except Exception:  # noqa: BLE001
        pass
    return SPAWN_UID


def _raggiungibile(path: str, uid: int) -> bool | None:
    """`True` se l'uid legge `path`, `False` se no, `None` se non si è potuto
    verificare — e i tre esiti restano distinti: «non so» non è «al sicuro»."""
    if not os.path.isdir(path):
        return None
    try:
        r, w = os.pipe()
        pid = os.fork()
    except Exception:  # noqa: BLE001 — niente fork: non si conclude
        return None
    if pid == 0:
        os.close(r)
        try:
            os.setgroups([])
            os.setgid(SPAWN_GID)
            os.setuid(uid)
            os.listdir(path)
            os.write(w, b"1")
        except Exception:  # noqa: BLE001
            os.write(w, b"0")
        finally:
            os.close(w)
        os._exit(0)
    os.close(w)
    try:
        esito = os.read(r, 1)
    finally:
        os.close(r)
        os.waitpid(pid, 0)
    return esito == b"1"


def check(loud: bool = True) -> dict:
    """Verifica il confine. Ritorna {path: True|False|None}."""
    if os.geteuid() != 0:
        LOG.info("verifica del confine saltata: serve root per assumere l'uid "
                 "di uno spawn")
        return {}
    uid = _spawn_uid()
    esiti: dict = {}
    for path, perche in VIETATI.items():
        esiti[path] = _raggiungibile(path, uid)
        if esiti[path] is True and loud:
            LOG.error(
                "CONFINE ROTTO · uno spawn (uid %s) legge %s — %s. Questa "
                "protezione è il permesso della directory, non logica "
                "applicativa: se è caduta, è caduta per come l'istanza è "
                "montata.", uid, path, perche)
        elif esiti[path] is None and loud:
            LOG.warning("confine non verificabile su %s (assente o fork negato)",
                        path)
    if loud and all(v is False for v in esiti.values()) and esiti:
        LOG.info("confine verificato: uno spawn (uid %s) non raggiunge vault, "
                 "topic store né seed", uid)
    return esiti


# ── Content stores (clodia-platform#704) ─────────────────────────────────────
#
# What someone WROTE also lives outside the topic store, in files the
# agent-server keeps for itself: the activity log (`run_started` carries the
# prompt's first 160 characters, `run_done` the reply up to 4000), the
# persisted transcripts of every chat, the stored reasoning, and the
# agent-server log. Until #704 the first two and the log were created with the
# default umask — directories 755, files 644 — on the shared datadir, so ANY
# spawn uid, sysadmin's `Bash` included, could `cat` them on a Linux host. On
# a macOS host the datadir is a virtiofs bind and the bits are not enforced at
# all (#544): there no chmod helps, and the store must move to a Docker volume.
#
# Two halves, like the rest of this file: `harden_content_stores` protects them
# (root-only, or other-bits-off for the stores a group of spawns shares by
# design, see `_STORES`) where the filesystem enforces it, and `check_content`
# asks the question as a spawn uid and logs ERROR when one is still readable —
# which is exactly what it will say on a virtiofs bind.
#
# What stays readable by a spawn, on purpose: its own spawn dir (its uid,
# 700); `/datadir/spawns` itself only to traverse (711); the seed memory its
# spawn dir links to (shared by the seed's gid); `plugins/`,
# `packs/`, the skill/rule/constitution catalogues and the code trees
# (`/clodia` minus `sessions/`, `/platform-src`) it reads to work; the
# `agency-shared/` card handoff; `runtime/opencode-data/<seed>` to its seed's
# spawns and `data/codex-home` to codex spawns (group). Nothing above is
# touched by the hardening. `/datadir/agents` and the vault keep the boundary
# `check()` verifies (NOT readable), unchanged.

DIR_MODE = 0o700
FILE_MODE = 0o600
SPAWNS_ROOT_MODE = 0o711

#: How a store is protected:
#: - `private`: root only — dirs 700, files 600;
#: - `group`: shared by design with a group of spawns (the seed's gid for
#:   opencode, the codex gid for CODEX_HOME), so only the OTHER bits go —
#:   another seed's spawn reads nothing, the owning group keeps working;
#: - `spawns`: the root is traverse-only (711: a spawn reaches its own dir,
#:   cannot list the others), each spawn dir loses its other bits; the spawn
#:   dirs themselves are the spawn's (uid + 700 from `_sandbox_prepare`).
PRIVATE, GROUP, SPAWNS = "private", "group", "spawns"

#: gid of the probe for the content stores: outside the seed range
#: (`_SANDBOX_GID_BASE` … +1000) and the codex gid (+1000), so «denied» means
#: «a spawn of ANOTHER seed reads nothing», not «this gid happened to own it».
PROBE_GID = 65531

#: (path relative to the datadir — globs allowed —, mode, created if missing, why)
_STORES = (
    ("agent-state/activity", PRIVATE, True,
     "il log di attività: prompt (160 car.) e risposte (fino a 4000) di ogni turno"),
    ("agent-state/transcripts", PRIVATE, True, "le trascrizioni persistite di ogni chat"),
    ("agent-state/reasoning", PRIVATE, True,
     "il ragionamento salvato, che cita i contenuti del canale"),
    ("logs", PRIVATE, True,
     "i log dell'agent-server (anche i ruotati .1-.3) e del gateway"),
    ("data/*-sessions", PRIVATE, False,
     "la cronologia delle chat registrata dal server (chat-*.jsonl, ophelia compresa)"),
    # Clodia's own chat history is NOT in the datadir: `KIND_SESSIONS_DIR`
    # puts it under the code tree, which spawns read on purpose (`/clodia`).
    ("@bundle/sessions", PRIVATE, False,
     "la cronologia delle chat di clodia, dentro l'albero del codice (/clodia/sessions)"),
    ("jobs", PRIVATE, False, "i job schedulati, con i loro prompt"),
    ("job_proposals", PRIVATE, False, "le proposte di job, con i loro prompt"),
    ("mail-courier-state", PRIVATE, False, "lo stato del corriere mail: buste e mittenti"),
    ("mail-triggers", PRIVATE, False, "i trigger mail dei topic, con i loro prompt"),
    ("channel-relay-state", PRIVATE, False, "lo stato del relay dei canali: messaggi in transito"),
    ("routing", PRIVATE, False, "le decisioni e le correzioni del router dei messaggi"),
    ("claude-home", PRIVATE, False,
     "la home del CLI claude dell'orchestratore: sessioni e credenziali OAuth"),
    ("agent-workspaces", PRIVATE, False, "i workspace legacy degli agenti (pre-spawn)"),
    ("data/codex-home", GROUP, False,
     "CODEX_HOME condiviso dagli spawn codex: login e sessioni (rollout) dei turni"),
    ("runtime/opencode-data", GROUP, False,
     "le sessioni di opencode, una cartella per seed (gid del seed)"),
    ("spawns", SPAWNS, False,
     "le cartelle degli spawn: trascrizioni Claude in .claude/projects/*.jsonl"),
)


def _datadir():
    from ..config import data_path
    return data_path("")


def store_specs() -> list[tuple[str, str, bool, str]]:
    """(absolute path, mode, create, why) of every store present, globs
    expanded. Resolved at call time: tests and clones move `CLODIA_DATA`."""
    import glob
    from ..config import WORKSPACE_ROOT
    root = str(_datadir())
    out = []
    for rel, mode, create, why in _STORES:
        if rel.startswith("@bundle/"):
            pattern = os.path.join(str(WORKSPACE_ROOT), rel[len("@bundle/"):])
        else:
            pattern = os.path.join(root, rel)
        if any(c in rel for c in "*?["):
            for p in sorted(glob.glob(pattern)):
                if os.path.isdir(p) and not os.path.islink(p):
                    out.append((p, mode, False, why))
        elif create or (os.path.isdir(pattern) and not os.path.islink(pattern)):
            out.append((pattern, mode, create, why))
    return out


def content_stores() -> dict:
    """{path: why} of the content stores present (see `store_specs`)."""
    return {p: why for p, _m, _c, why in store_specs()}


def _walk(path: str):
    """Every entry under `path`, never following a symlink (an agent could
    plant one to make root chmod an arbitrary file)."""
    for root, dirs, files in os.walk(path, followlinks=False):
        for d in dirs:
            yield os.path.join(root, d), True
        for f in files:
            yield os.path.join(root, f), False


def _private(path: str) -> None:
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    os.chmod(path, DIR_MODE)
    for p, is_dir in _walk(path):
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode) or not st.st_mode & 0o077:
            continue
        os.chmod(p, DIR_MODE if is_dir else FILE_MODE)


def _no_other(path: str) -> None:
    for p, _is_dir in [(path, True), *_walk(path)]:
        st = os.lstat(p)
        if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o007:
            os.chmod(p, stat.S_IMODE(st.st_mode) & ~0o007)


def _spawns(path: str) -> None:
    os.chmod(path, SPAWNS_ROOT_MODE)
    for name in os.listdir(path):
        p = os.path.join(path, name)
        st = os.lstat(p)
        if stat.S_ISDIR(st.st_mode) and st.st_mode & 0o007:
            os.chmod(p, stat.S_IMODE(st.st_mode) & ~0o007)


_HARDEN = {PRIVATE: _private, GROUP: _no_other, SPAWNS: _spawns}


def harden_content_stores() -> dict:
    """Protect every content store per its mode. {path: "ok" | error}. Root
    only: without it there is nobody to protect them from."""
    if os.geteuid() != 0:
        return {}
    out = {}
    for path, mode, _create, _why in store_specs():
        try:
            _HARDEN[mode](path)
            out[path] = "ok"
        except OSError as e:
            out[path] = f"{type(e).__name__}: {e}"[:200]
            LOG.error("archivio di contenuti non protetto: %s (%s)", path, out[path])
    return out


def _sentinel(directory: str) -> str | None:
    """A 0600 root file inside `directory`, for the probe to try to read: a
    directory listing alone would say nothing about files under a 711 dir."""
    try:
        path = os.path.join(directory, ".boundary-probe")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
        try:
            os.fchmod(fd, FILE_MODE)
            os.write(fd, b"probe")
        finally:
            os.close(fd)
        return path
    except OSError:
        return None


def _drop(uid: int, gid: int) -> None:
    """Lose root for good in the probe's child. A seam for the tests, which
    cannot change uid: they replace it and keep the REAL fork and reads."""
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def _legge(path: str, uid: int, gid: int = PROBE_GID, *, sentinel: bool = True) -> bool | None:
    """`True` if (uid, gid) lists `path` OR reads a 0600 root file in it,
    `False` if neither, `None` if it could not be verified."""
    if not os.path.isdir(path):
        return None
    sentinella = _sentinel(path) if sentinel else None
    try:
        r, w = os.pipe()
        pid = os.fork()
    except Exception:  # noqa: BLE001
        if sentinella:
            os.unlink(sentinella)
        return None
    if pid == 0:
        os.close(r)
        esito = b"0"
        try:
            _drop(uid, gid)
            try:
                os.listdir(path)
                esito = b"1"
            except Exception:  # noqa: BLE001
                pass
            if sentinella:
                try:
                    with open(sentinella, "rb") as fh:
                        fh.read(1)
                    esito = b"1"
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 — could not drop privileges
            esito = b"?"
        finally:
            os.write(w, esito)
            os.close(w)
        os._exit(0)
    os.close(w)
    try:
        esito = os.read(r, 1)
    finally:
        os.close(r)
        os.waitpid(pid, 0)
        if sentinella:
            try:
                os.unlink(sentinella)
            except OSError:
                pass
    return {b"1": True, b"0": False}.get(esito)


#: Spawn dirs probed per boot: one is the proof, a few cover a mixed state.
_SPAWN_SAMPLES = 3


def _probe(path: str, mode: str, uid: int) -> bool | None:
    if mode != SPAWNS:
        return _legge(path, uid)
    # The root: listing it would enumerate the others. Then a few spawn dirs,
    # each probed as a spawn uid that is NOT its owner.
    esiti = [_legge(path, uid)]
    try:
        figli = sorted(n for n in os.listdir(path)
                       if os.path.isdir(os.path.join(path, n))
                       and not os.path.islink(os.path.join(path, n)))[:_SPAWN_SAMPLES]
    except OSError:
        figli = []
    for n in figli:
        p = os.path.join(path, n)
        owner = os.stat(p).st_uid
        esiti.append(_legge(p, uid if owner != uid else uid + 1, sentinel=False))
    if any(e is True for e in esiti):
        return True
    return None if any(e is None for e in esiti) else False


def check_content(loud: bool = True) -> dict:
    """As a spawn uid of no seed's group: can any content store be read?
    {path: True|False|None}."""
    if os.geteuid() != 0:
        LOG.info("verifica degli archivi di contenuti saltata: serve root")
        return {}
    uid = _spawn_uid()
    esiti: dict = {}
    for path, mode, _create, perche in store_specs():
        esiti[path] = _probe(path, mode, uid)
        if esiti[path] is True and loud:
            LOG.error(
                "CONTENUTI LEGGIBILI · uno spawn (uid %s) legge %s — %s "
                "(clodia-platform#704). Su un bind virtiofs da macOS i bit non "
                "valgono: l'archivio va su un volume Docker.", uid, path, perche)
        elif esiti[path] is None and loud:
            LOG.warning("archivio di contenuti non verificabile: %s", path)
    if loud and esiti and all(v is False for v in esiti.values()):
        LOG.info("archivi di contenuti verificati: uno spawn (uid %s) non ne "
                 "legge nessuno (%d)", uid, len(esiti))
    return esiti


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    r = check()
    c = check_content()
    sys.exit(1 if any(v is True for v in (*r.values(), *c.values())) else 0)
