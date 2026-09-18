"""Setup di un pack come funzione, non come turno di un agente.

`pack_ops.py` consegna la riconciliazione (`requires`/`datastores`/
`rag_collections`/`mcp_servers`) a un turno di `sysadmin`, che LEGGE il prompt
e decide da sé quali tool chiamare. Le dichiarazioni sono però dati strutturati
(un dict, non prosa): per la parte meccanica — installare un pacchetto,
creare una collection — non serve un LLM che interpreti, serve iterare.

Nato dal bottone "Aggiorna tutto" (18 set 2026, richiesta di Davide): un click
admin aggiorna N pack e fa il loro setup senza aprire N turni di chat. Il
principal di questa funzione è sempre l'ADMIN che ha cliccato — stesso pattern
di `pack_mcp_mount.auto_mount_imported_mcp(trusted=True)`: il click è già
l'approvazione, quindi i verbi gated (`packs.install_pip`/`install_npm`)
eseguono per ruolo umano via `/internal/tool` (`_human_tool_allowed`), non
aprono una card in chat.

Cosa NON fa questa funzione, di proposito: `rag.ingest` di una risorsa
richiede che il file stia già DENTRO un topic (`tier`+`name`+`path`) — non un
path arbitrario sul disco del gateway. Portarcelo (scaricare un `url`,
depositare un `path` del pack in un topic scelto a caso) è una decisione su
QUALE topic e CHI lo vede che questa funzione non deve prendere da sola:
resta un gap riportato, non un'azione automatica. La collection si crea
comunque (`rag.create_collection`, nessun contenuto sensibile), l'ingest resta
al reconciler agentico o all'owner.
"""
from __future__ import annotations

import asyncio
import logging

from . import gateway_pdp

LOG = logging.getLogger("agent-server.pack_ops_logical")


def _pip_npm(kind: str, verb: str, packages: list, principal: str, done: list, gaps: list) -> None:
    for pkg in packages:
        pkg = str(pkg or "").strip()
        if not pkg:
            continue
        try:
            status, data = gateway_pdp.gw_tool(verb, {"packages": [pkg]}, principal)
        except Exception as e:  # noqa: BLE001 — un guasto di rete non è un giudizio
            gaps.append({"kind": kind, "package": pkg, "detail": f"chiamata fallita: {e}"})
            continue
        result = (data or {}).get("result") or {}
        if status == 200 and result.get("ok"):
            done.append(f"{kind}:{pkg}")
        else:
            detail = result.get("stderr_tail") or (data or {}).get("detail") or (data or {}).get("error") or f"HTTP {status}"
            gaps.append({"kind": kind, "package": pkg, "detail": str(detail)[:300]})


def _bin_checks(commands: list, principal: str, done: list, gaps: list) -> None:
    """Read-only: `check_command` non installa nulla, un binario di sistema non
    si mette da un pacchetto pip/npm. Serve solo a dire se manca."""
    for cmd in commands:
        cmd = str(cmd or "").strip()
        if not cmd:
            continue
        try:
            status, data = gateway_pdp.gw_tool("packs.check_command", {"command": cmd}, principal)
        except Exception as e:  # noqa: BLE001
            gaps.append({"kind": "bin", "command": cmd, "detail": f"chiamata fallita: {e}"})
            continue
        result = (data or {}).get("result") or {}
        if status == 200 and result.get("found"):
            done.append(f"bin:{cmd}")
        else:
            gaps.append({"kind": "bin", "command": cmd,
                        "detail": "non trovato nel runtime del gateway — non installabile da qui, va nell'immagine"})


def _rag_collections(collections: list, principal: str, done: list, gaps: list) -> None:
    for coll in collections:
        name = str((coll or {}).get("name") or "").strip()
        if not name:
            continue
        tier = (coll or {}).get("tier") or "SEAL-1"
        try:
            status, data = gateway_pdp.gw_tool(
                "rag.create_collection",
                {"collection": name, "tier": tier,
                 "description": (coll or {}).get("description") or ""},
                principal)
        except Exception as e:  # noqa: BLE001
            gaps.append({"kind": "rag_collection", "collection": name, "detail": f"chiamata fallita: {e}"})
            continue
        errore = str((data or {}).get("error") or "").lower()
        # Idempotente per costruzione lato gateway (create su una collection già
        # esistente non è un errore): sia 200 che un esito "già esiste" contano
        # come fatto, non come gap.
        if status == 200 or "esiste" in errore:
            done.append(f"rag_collection:{name}")
        else:
            gaps.append({"kind": "rag_collection", "collection": name,
                        "detail": str((data or {}).get("error") or f"HTTP {status}")[:300]})
        resources = (coll or {}).get("resources") or []
        if resources:
            # L'ingest richiede un file già DENTRO un topic (tier+name+path): un
            # path del pack o un url non ce l'hanno. Deciso: gap, non un topic
            # di comodo inventato qui.
            gaps.append({"kind": "rag_ingest", "collection": name,
                        "detail": f"{len(resources)} risorse iniziali da ingerire "
                                 f"manualmente (richiede un topic da cui leggerle: "
                                 f"non automatizzato)"})


def run_logical_setup(name: str, decls: dict, principal: str) -> dict:
    """Provisioning deterministico di UN pack dalle sue dichiarazioni.

    Ritorna `{name, done: [...], gaps: [...]}`. `gaps` vuoto → il chiamante può
    marcare `setup_done`; non vuoto → resta pendente, con il MOTIVO per cui.
    """
    done: list = []
    gaps: list = []
    req = decls.get("requires") or {}
    _pip_npm("pip", "packs.install_pip", req.get("pip") or [], principal, done, gaps)
    _pip_npm("npm", "packs.install_npm", req.get("npm") or [], principal, done, gaps)
    _bin_checks((req.get("bin") or []) + (req.get("system") or []), principal, done, gaps)
    _rag_collections(decls.get("rag_collections") or [], principal, done, gaps)
    # datastores: nessuna azione qui di proposito — il file arriva con
    # l'import/update stesso (vedi SETUP.md dei pack), non è un passo separato.
    # mcp_servers: nessuna azione qui di proposito — il mount trusted per un
    # update first-party lo fa già `pack_mcp_mount.auto_mount_imported_mcp`
    # dentro `_perform_update`, prima che questa funzione sia chiamata.
    return {"name": name, "done": done, "gaps": gaps}


async def run_logical_setup_async(name: str, decls: dict, principal: str) -> dict:
    """`run_logical_setup` per i chiamanti `async def`: le chiamate al gateway
    sono sincrone (`requests`), farle dritte da un handler async fermerebbe
    l'event loop di tutto il processo per la durata di un pip install."""
    return await asyncio.to_thread(run_logical_setup, name, decls, principal)
