"""La dichiarazione di un seed contro ciò che il gateway custodisce davvero.

La quinta gamba, e mancava. `skill_sync`, `rule_sync`, `constitution_sync` e
`seed_sync` riconciliano il pack con la datadir a ogni avvio; la gamba
**dichiarazione → gateway** non ne ha mai avuta una. `register_agent` si chiamava
da un posto solo — l'import di un pack — quindi `tool_permissions`,
`gated_tools`, `profile_tools` e `denied_tools` raggiungevano l'autorità **una
volta sola**, e da lì in poi `config.yaml` del gateway restava la fotografia di
quel giorno mentre il file del seed diventava una dichiarazione che nessuno
rilegge (clodia-platform#203).

Tre guasti reali sono usciti di lì, e sono tutti e tre *silenziosi*:

1. la rotta di registrazione ha risposto **500 per quattro giorni** senza che
   nessuno se ne accorgesse — perché niente ri-registra, quindi nessun percorso
   sano la esercitava;
2. `ophelia` ha continuato a tenere `['*']` nella config del gateway dopo che il
   wildcard era stato ritirato;
3. un confinamento dichiarato nel seed non ha cambiato niente finché qualcuno
   non ha riscritto il file a mano e chiamato `register_agent` a mano, su due
   istanze.

**Questo modulo SEGNALA, non risolve.** Un push al boot renderebbe il seed
autoritativo sulla config viva, e un'entry ritoccata a runtime — ce ne sono, e
non sono documentate da nessuna parte — sparirebbe al riavvio successivo senza
che nessuno lo veda. La decisione presa è l'altra: la divergenza si annuncia, e
qualcuno decide. Ogni guasto qui sopra sarebbe stato colto da una divergenza che
si annuncia; nessuno sarebbe stato colto da un overwrite muto.

**L'unica eccezione è `unregistered`**, e non è un'eccezione alla regola: è fuori
dal suo dominio. Se l'agente non c'è affatto nella config del gateway non esiste
nessuna scelta a runtime da proteggere — c'è una registrazione che è **fallita**,
e l'agente non ha «meno verbi», ne ha zero (`agent_config()` solleva e la lista
torna vuota: si vede nel pannello, entra nei canali, parla, e non può fare
niente). Completare un'operazione fallita non è sovrascrivere una decisione. Ma
la riparazione **non è mai silenziosa quanto un esito pulito**: resta un WARNING
che dice che è stata riparata, altrimenti l'auto-repair diventa il nuovo modo di
nascondere il guasto a monte — che è precisamente il guasto n.1 qui sopra.
"""
from __future__ import annotations

import logging

from .loader import registry

LOG = logging.getLogger("agent-server.agents.gateway_drift")

#: Cosa si confronta, e sotto quale nome sui due lati. SOLO i quattro campi che
#: la config del gateway CUSTODISCE davvero.
#:
#: `carries` e `gated_in_channel` restano fuori di proposito: viaggiano con la
#: registrazione ma il gateway non li conserva — `upsert_agent` non ha il primo
#: fra i suoi kwarg, `agents_api.register` scarta il secondo (ritirato dal
#: dispatch il 7 ago 2026). Confrontarli produrrebbe una divergenza permanente e
#: falsa su OGNI agente, cioè un rilevatore che nessuno rilegge dopo la seconda
#: volta.
#:
#: (Che `carries` sia una dichiarazione che nessuno trasporta è un difetto suo,
#: della stessa famiglia di questa issue. Non si tratta qui.)
CAMPI: tuple[tuple[str, str], ...] = (
    ("tool_permissions", "allowed_tools"),
    ("gated_tools", "gated_tools"),
    ("denied_tools", "denied_tools"),
    ("profile_tools", "profile_tools"),
)


def _lista(v) -> list[str]:
    """`None` («il seed non si pronuncia») e assente si leggono come vuoto.

    Non è una perdita di informazione: all'enforcement il gateway legge
    `spec.get(campo) or []`, quindi assente e vuoto si comportano identici. Chi
    confronta non deve vedere una divergenza dove il comportamento è lo stesso.
    """
    return [str(x) for x in (v or [])]


def _differenza(dichiarato: list[str], registrato: list[str]) -> dict | None:
    """Divergenza fra due liste, o `None`.

    L'ORDINE non è drift: `native_tools` in ordine diverso non cambia niente di
    ciò che un agente può fare, e un rilevatore che segnala un riordino è un
    rilevatore che nessuno rilegge. I DUPLICATI nemmeno, per la stessa ragione.
    """
    d, r = set(dichiarato), set(registrato)
    if d == r:
        return None
    entrate = sorted(d - r)      # dichiarate e non registrate
    uscite = sorted(r - d)       # registrate e non dichiarate
    if not r:
        stato = "missing"        # il seed dichiara, il gateway non ha niente
    elif not d:
        stato = "extra"          # solo nel gateway: `ophelia` con ['*']
    else:
        stato = "changed"
    return {"status": stato, "declared_only": entrate, "registered_only": uscite}


def confronta(spec, registrazione: dict | None) -> dict:
    """Esito per UN agente. `registrazione=None` → il gateway non ha risposto.

    `unavailable` non si legge come «nessuna divergenza»: dire pulito perché non
    si è potuto guardare è la bugia esatta che questa riconciliazione esiste per
    togliere.
    """
    nome = getattr(spec, "name", "") or ""
    if registrazione is None:
        return {"agent": nome, "status": "unavailable", "fields": []}
    if not registrazione.get("registered"):
        return {"agent": nome, "status": "unregistered", "fields": []}
    campi = []
    for seed_key, gw_key in CAMPI:
        diff = _differenza(_lista(getattr(spec, seed_key, None)),
                           _lista(registrazione.get(gw_key)))
        if diff:
            campi.append({"field": seed_key, **diff})
    return {"agent": nome, "status": "diverged" if campi else "clean",
            "fields": campi}


def _e_agente(spec) -> bool:
    """Un umano non sta nella config del gateway, e non è una dimenticanza: la
    sua matrice vive nel seed, dove il confine lo mette il kernel (§3.5). Senza
    questa riga ogni persona risulterebbe `unregistered` — e la riparazione la
    registrerebbe come agente, cioè inventerebbe un principal di un tipo che il
    modello non ha. Stessa regola di `gateway_pdp.is_agent`.
    """
    return getattr(spec, "type", "") != "human"


def _ripara(spec) -> bool:
    """Registra un agente ASSENTE dalla config del gateway, con la stessa
    chiamata dell'install di un pack — una policy sola, non due."""
    from ..api import gateway_admin
    gateway_admin.register_agent(
        spec.name, list(getattr(spec, "tool_permissions", None) or []),
        gated_tools=getattr(spec, "gated_tools", None),
        gated_in_channel=getattr(spec, "gated_in_channel", None),
        profile_tools=getattr(spec, "profile_tools", None),
        carries=getattr(spec, "carries", None),
        denied_tools=getattr(spec, "denied_tools", None))
    return True


def check_all(repair: bool = True) -> dict:
    """Confronta ogni seed registrato col gateway. SINCRONA: fa I/O di rete, e
    va chiamata da un thread (`asyncio.to_thread`) — una POST sincrona dentro un
    handler async ferma l'event loop di tutto il processo (#106).
    """
    from ..api import gateway_admin
    righe: list[dict] = []
    for spec in registry.list():
        if not _e_agente(spec):
            continue
        try:
            reg = gateway_admin.registration(spec.name)
        except Exception as e:  # noqa: BLE001
            LOG.warning("drift: registrazione di '%s' non leggibile (%s)",
                        spec.name, str(e)[:120])
            reg = None
        riga = confronta(spec, reg)
        if riga["status"] == "unregistered" and repair:
            # Non c'è nessuna decisione da proteggere: l'entry manca per una
            # registrazione fallita, non per una scelta.
            try:
                _ripara(spec)
                riga["repaired"] = True
            except Exception as e:  # noqa: BLE001
                riga["repaired"] = False
                riga["error"] = str(e)[:160]
        righe.append(riga)
    return {"checked": len(righe), "agents": righe}


def report_at_boot() -> dict:
    """Confronta e SCRIVE il risultato nel log. Non solleva mai: una diagnosi
    che impedisce l'avvio è peggio della diagnosi che manca."""
    try:
        rep = check_all()
    except Exception as e:  # noqa: BLE001
        LOG.warning("confronto seed↔gateway non eseguito: %s", e)
        return {"checked": 0, "agents": []}
    for riga in rep["agents"]:
        nome, stato = riga["agent"], riga["status"]
        if stato == "clean":
            continue
        if stato == "unregistered":
            if riga.get("repaired"):
                # Riparato ≠ pulito, e va detto con le stesse lettere di un
                # guasto: se l'auto-repair diventa muto, il fallimento a monte
                # dell'import torna invisibile — ed è il guasto che ha lasciato
                # un agente senza verbi per quattro giorni.
                LOG.warning("seed '%s': NON era registrato nel gateway (zero verbi) "
                            "— RIPARATO AUTOMATICAMENTE al boot. La registrazione "
                            "all'import era fallita: se si ripete a ogni avvio, il "
                            "problema è l'import", nome)
            else:
                LOG.error("seed '%s': NON registrato nel gateway (zero verbi) e la "
                          "riparazione è fallita (%s) — l'agente si vede nel "
                          "pannello e non può fare niente",
                          nome, riga.get("error", "?"))
            continue
        if stato == "unavailable":
            LOG.warning("seed '%s': la registrazione nel gateway non è leggibile — "
                        "divergenza NON verificata (non «nessuna divergenza»)", nome)
            continue
        for c in riga["fields"]:
            LOG.warning("seed '%s' · %s %s: dichiarati e non registrati %s; "
                        "registrati e non dichiarati %s",
                        nome, c["field"], c["status"],
                        c["declared_only"] or "-", c["registered_only"] or "-")
    divergenti = [r["agent"] for r in rep["agents"] if r["status"] == "diverged"]
    if divergenti:
        LOG.warning("seed↔gateway: %d seed su %d divergono dalla config del "
                    "gateway: %s. Nessuno è stato sovrascritto — la config viva "
                    "può contenere modifiche intenzionali, e la scelta è di chi "
                    "legge", len(divergenti), rep["checked"], ", ".join(divergenti))
    else:
        LOG.info("seed↔gateway: %d seed allineati", rep["checked"])
    return rep
