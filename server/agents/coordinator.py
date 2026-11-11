"""Il coordinatore di uno scope — dichiarato, non dedotto dal rango.

Fino a qui il ripiego del router era `rank.highest(ai)`: quando nessuno matchava,
rispondeva il più alto in rango. Due difetti in una riga sola (R10,
clodia-platform#188):

- **risponde invece di decidere.** R10 chiede una *classificazione* fatta da un
  modello dove i coseni hanno rinunciato — e l'esito può benissimo essere «questa
  è per Aitiero», che il rango non sa produrre;
- **nessuno lo ha deciso.** «normalmente clodia» era vero solo come effetto
  collaterale del suo essere la più anziana in quasi tutte le stanze. Cambia un
  rango, o costruisci una stanza senza di lei, e il ripiego si sposta senza che
  nessuno se ne accorga.

La regola qui sotto è la ruling dell'11 ago 2026 (router-notebook, «Four
decisions closing R10, R11, R14, R15»):

> «il coordinatore è sempre il segretario per i topic a meno che non sia presente
> clodia e in quel caso è lei»

e il notebook aggiunge la ragione per cui questo file non introduce un campo
`coordinator: true` in `agent.yaml`: *«No new field is needed anywhere: the rule
reads the participant list»*. Un flag per-seed sarebbe anche una delega
all'auto-attribuzione — ogni seed potrebbe nominarsi coordinatore modificando il
proprio file. Qui la lista è **una**, ordinata, e si legge con un grep.

**Perché è un modulo e non una funzione dentro `channels.py`.** La stessa
precedenza era già scritta a mano in `_select_topic_intro_agent` (chi introduce
un topic nuovo): due copie della stessa regola divergono al primo cambiamento, e
la seconda non la aggiorna nessuno perché non sa di esistere. Ora il punto è uno.

L'idoneità NON si decide qui: chi chiama passa spec già filtrate per
provider/SEAL ≥ tier del topic. Così il caso A4 dell'agents-notebook — «se il
tier dello scope supera quello che può usare clodia allora segretario subentra,
in quanto all-tier» — cade fuori gratis, senza una seconda regola che lo dica.
"""
from __future__ import annotations

import logging

LOG = logging.getLogger("agent-server.agents.coordinator")

#: The seeds that can orchestrate a room, in FALLBACK order. Since
#: clodia-platform#667 (owner, 11 Nov 2026) the room CHOOSES its orchestrator
#: (`meta.orchestrator`, one click in the participants list) and this order only
#: decides when the choice is absent or cannot serve (not a participant, not
#: eligible at the room's tier). The default is the secretary everywhere;
#: before #667 it was «clodia if present» (ruling of 11 Aug 2026).
DECLARED: tuple[str, ...] = ("segretario", "clodia", "ophelia")

#: The meta key holding the room's choice.
META_KEY = "orchestrator"


def room_choice(meta: dict | None) -> str | None:
    """The orchestrator the room chose, if it is one that can orchestrate."""
    v = str((meta or {}).get(META_KEY) or "").strip().lower()
    return v if v in DECLARED else None


def pick(specs, preferred: str | None = None) -> tuple[object | None, str]:
    """Il coordinatore fra `specs`, con la ragione della scelta.

    `specs` sono le spec degli agenti AI **già idonei** al tier del topic.
    `preferred` is the room's choice (`room_choice(meta)`): it wins when it is
    among them; otherwise the DECLARED order, and the reason says so.
    Ritorna `(spec, reason)`, oppure `(None, reason)` se nessun coordinatore
    dichiarato è partecipante idoneo — caso in cui chi chiama decide la propria
    rete di sicurezza, e deve dirlo ad alta voce.
    """
    by_name = {getattr(s, "name", None): s for s in (specs or []) if s is not None}
    if preferred and by_name.get(preferred) is not None:
        return by_name[preferred], f"orchestratore scelto dalla stanza ({preferred})"
    if preferred:
        LOG.warning("orchestratore scelto %s non disponibile fra gli idonei (%s): ripiego",
                    preferred, ", ".join(n for n in by_name if n) or "nessuno")
    for name in DECLARED:
        spec = by_name.get(name)
        if spec is not None:
            return spec, f"coordinatore dichiarato ({name})"
    LOG.warning(
        "nessun coordinatore dichiarato fra i partecipanti idonei (%s): "
        "attesi uno di %s",
        ", ".join(n for n in by_name if n) or "nessuno",
        ", ".join(DECLARED),
    )
    return None, "nessun coordinatore dichiarato fra i partecipanti idonei"
