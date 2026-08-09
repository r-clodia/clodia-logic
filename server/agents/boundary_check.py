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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    r = check()
    sys.exit(1 if any(v is True for v in r.values()) else 0)
