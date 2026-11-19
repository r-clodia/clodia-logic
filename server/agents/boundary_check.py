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
# Two halves, like the rest of this file: `harden_content_stores` makes them
# root-only (directories 700, files 600) where the filesystem enforces it, and
# `check_content` asks the question as a spawn uid and logs ERROR when one is
# still readable — which is exactly what it will say on a virtiofs bind.

DIR_MODE = 0o700
FILE_MODE = 0o600


def content_stores() -> dict:
    """{path: why} of the agent-server's own content stores. Resolved from
    `CLODIA_DATA` at call time, not at import: tests and clones move it."""
    from ..config import data_path
    return {
        str(data_path("agent-state") / "activity"):
            "il log di attività: prompt (160 car.) e risposte (fino a 4000) di ogni turno",
        str(data_path("agent-state") / "transcripts"):
            "le trascrizioni persistite di ogni chat",
        str(data_path("agent-state") / "reasoning"):
            "il ragionamento salvato, che cita i contenuti del canale",
        str(data_path("logs")):
            "i log dell'agent-server e del gateway (righe di turno, decisioni)",
    }


def _private(path: str) -> None:
    """chmod one tree to root-only, never following a symlink (an agent could
    plant one to make root chmod an arbitrary file)."""
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    os.chmod(path, DIR_MODE)
    for root, dirs, files in os.walk(path, followlinks=False):
        for d in dirs:
            p = os.path.join(root, d)
            if not os.path.islink(p) and os.stat(p).st_mode & 0o077:
                os.chmod(p, DIR_MODE)
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p) and os.stat(p).st_mode & 0o077:
                os.chmod(p, FILE_MODE)


def harden_content_stores() -> dict:
    """Make every content store root-only. {path: "ok" | error}. Root only:
    without it there is nobody to protect them from, and nothing to change."""
    if os.geteuid() != 0:
        return {}
    out = {}
    for path in content_stores():
        try:
            _private(path)
            out[path] = "ok"
        except OSError as e:
            out[path] = f"{type(e).__name__}: {e}"[:200]
            LOG.error("archivio di contenuti non reso privato: %s (%s)", path, out[path])
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


def _legge(path: str, uid: int) -> bool | None:
    """`True` if the uid lists `path` OR reads a 0600 root file in it,
    `False` if neither, `None` if it could not be verified."""
    if not os.path.isdir(path):
        return None
    sentinella = _sentinel(path)
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
            os.setgroups([])
            os.setgid(SPAWN_GID)
            os.setuid(uid)
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


def check_content(loud: bool = True) -> dict:
    """As a spawn uid: can any content store be read? {path: True|False|None}."""
    if os.geteuid() != 0:
        LOG.info("verifica degli archivi di contenuti saltata: serve root")
        return {}
    uid = _spawn_uid()
    esiti: dict = {}
    for path, perche in content_stores().items():
        esiti[path] = _legge(path, uid)
        if esiti[path] is True and loud:
            LOG.error(
                "CONTENUTI LEGGIBILI · uno spawn (uid %s) legge %s — %s "
                "(clodia-platform#704). Su un bind virtiofs da macOS i bit non "
                "valgono: l'archivio va su un volume Docker.", uid, path, perche)
        elif esiti[path] is None and loud:
            LOG.warning("archivio di contenuti non verificabile: %s", path)
    if loud and esiti and all(v is False for v in esiti.values()):
        LOG.info("archivi di contenuti verificati: uno spawn (uid %s) non legge "
                 "attività, trascrizioni, ragionamento né log", uid)
    return esiti


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    r = check()
    c = check_content()
    sys.exit(1 if any(v is True for v in (*r.values(), *c.values())) else 0)
