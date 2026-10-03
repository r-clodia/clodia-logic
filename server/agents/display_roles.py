"""Il ruolo MOSTRATO di chi sta in una stanza — uno solo, derivato sul server.

La lista dei partecipanti diceva `contributor` per tutti. In una stanza come
`SEAL-2/titul-brightnode`, clodia, segretario, messaggero, sysadmin e avvocato
leggevano la stessa parola: la pagina non diceva chi possiede la stanza, chi la
conduce, e chi ci sta per un ruolo che è della colonia e non di quel topic
(clodia-platform#497).

**Perché sta sul server e non nella webui.** Il badge lo guardano tre client —
la webui, la PWA, e chiunque legga il canale via API — e tre derivazioni della
stessa cosa sono tre risposte che divergono al primo seed nuovo. Qui la
funzione è pura: riceve il meta della stanza, chi coordina e gli attributi dei
seed, e restituisce righe già ordinate.

**Cosa NON fa, ed è la parte da non perdere di vista.** Non autorizza niente.
Il `role` tecnico (`owner`/`contributor`/`reader`) resta quello di prima,
viaggia nella stessa riga, e le regole d'accesso (clearance + partecipazione)
non guardano questo modulo. Un `reader` conserva il suo marchio di sola lettura
qualunque badge porti — se un flag decorasse *e* autorizzasse, un seed si
prenderebbe un permesso modificando il proprio file.

**Chi conduce non si decide qui.** `manager` arriva da fuori, e chi lo chiama lo
chiede a `coordinator.pick` — l'unico posto in cui la ruling dell'11 ago 2026 è
scritta («il coordinatore è sempre il segretario per i topic a meno che non sia
presente clodia e in quel caso è lei»). Riapplicarla qui sarebbe la seconda
copia, e la seconda copia è esattamente ciò che `coordinator.py` esiste per non
avere. Il vantaggio concreto: il caso A4 — stanza di tier superiore a quello che
clodia può servire — produce «segretario acting manager» senza una riga in più,
perché l'idoneità l'ha già applicata chi ha scelto il coordinatore.
"""
from __future__ import annotations

OWNER = "owner"
MANAGER = "manager"
STAFF = "staff"
CONTRIBUTOR = "contributor"

#: L'ordine in cui la stanza si legge. Sta qui e non nei client per la stessa
#: ragione del resto del modulo: tre ordinamenti divergono, uno no.
_PESO = {OWNER: 0, MANAGER: 1, STAFF: 2, CONTRIBUTOR: 3}

#: Ruoli tecnici riconosciuti. Qualunque altra cosa scritta nella mappa vale
#: `contributor`, che è ciò che «invitato» ha sempre significato.
_RUOLI_TECNICI = ("owner", "contributor", "reader")


def _ruolo_tecnico(meta: dict, chi: str) -> str:
    if chi and chi == meta.get("owner"):
        return "owner"
    raw = meta.get("participants")
    if isinstance(raw, dict):
        r = str(raw.get(chi) or "").strip().lower()
        return r if r in _RUOLI_TECNICI else "contributor"
    # Forma legacy: una LISTA vale tutta `contributor`.
    return "contributor"


def _nomi(meta: dict) -> list[str]:
    """Partecipanti più l'owner, senza duplicati e nell'ordine di dichiarazione.

    L'owner entra anche quando non è fra i `participants`: capita, ed è il modo
    in cui una stanza finiva per sembrare senza padrone.
    """
    raw = meta.get("participants")
    nomi = list(raw.keys()) if isinstance(raw, dict) else list(raw or [])
    owner = meta.get("owner")
    if owner:
        nomi.append(owner)
    return list(dict.fromkeys(n for n in nomi if n))


def display_roles(meta: dict, manager: str | None, seeds: dict) -> list[dict]:
    """Una riga per partecipante: ruolo mostrato, marchi, ruolo tecnico, ordine.

    `seeds` = {nome: spec} per i soli nomi che ne hanno una. Un umano invitato
    non ha un `agent.yaml` e non deve far saltare la derivazione: assente vuol
    dire «nessun attributo», cioè contributor.
    """
    righe: list[dict] = []
    for nome in _nomi(meta):
        spec = seeds.get(nome)
        e_staff = bool(getattr(spec, "staff", False))
        e_vice = bool(getattr(spec, "deputy", False))
        tecnico = _ruolo_tecnico(meta, nome)

        marks: list[str] = []
        if e_staff:
            marks.append("staff")
        if e_vice:
            marks.append("deputy")

        if nome == meta.get("owner"):
            # La proprietà della stanza non è un grado di accesso, ed è la riga
            # più in alto: vince su qualunque altro badge.
            mostrato = OWNER
        elif manager and nome == manager:
            mostrato = MANAGER
            # «acting manager» solo quando conduce in quanto VICE, cioè quando
            # il coordinatore dichiarato più alto non è nella stanza. Senza
            # questa distinzione il segretario leggerebbe «acting» anche dove
            # non sta sostituendo nessuno.
            if e_vice:
                marks.append("acting manager")
        elif e_staff:
            mostrato = STAFF
        else:
            mostrato = CONTRIBUTOR

        righe.append({
            "name": nome,
            "display_role": mostrato,
            "marks": marks,
            "role": tecnico,
            "readonly": tecnico == "reader",
            "sort": _PESO[mostrato],
        })
    righe.sort(key=lambda r: r["sort"])
    return righe
