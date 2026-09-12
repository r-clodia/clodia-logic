"""Member list delle collection RAG: l'indice INVERSO dei grant già dichiarati.

`clodia-platform#341` chiede che ogni datastore **o collection** mostri il
proprio clearance e la lista dei seed autorizzati. Per le collection il
clearance c'è già (`tier`, proiettato da `plugins.py` e `datastores.py`); la
member list sembrava non esistere nel modello — e invece esiste, solo scritta
nel verso opposto: è un seed a dichiarare `rag_read`/`rag_write`
(`server/agents/models.py`), quindi «chi raggiunge questa collection» è già
scritto, sparso su N file, uno per agente. Qui lo si gira: collection → seed.

**Proiezione, non nuovo modello.** L'alternativa era un campo `seeds:` nel
manifest del pack con gate fail-closed a valle: oggi nessun manifest lo
dichiara, quindi al merge spegnerebbe il RAG di ogni agente, e le collection
ORFANE (nessun pack installato che le dichiari) non potrebbero comunque
popolarlo. Qui non si concede e non si toglie nulla: l'autorità resta quella dei
seed e l'enforcement resta del gateway.

Due insiemi, e tenerli distinti è il punto:

- i **membri** — chi nomina quella collection in `rag_read`/`rag_write`;
- il **bypass** — chi la raggiunge senza nominarla, perché possiede l'intero
  namespace dei verbi (`rag.*`: sysadmin, il provisioner dei pack) o ha
  dichiarato `*` sull'asse RAG. Ripeterli dentro ogni riga direbbe due cose
  false: che sono membri di quella collection, e che la member list li governa.
  Tenerli fuori e basta ne direbbe una terza: che una `seeds_read` vuota
  significa «nessuno la legge».
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .inheritance import effective_tool_permissions
from .loader import registry
from .trifecta import _covers

#: Il namespace RAG per intero. Chi lo copre (`rag.*`, o un `*` globale) non ha
#: bisogno di essere membro di niente.
_RAG_NS = "rag.*"

#: Wildcard dentro `rag_read`/`rag_write`: non è il nome di una collection, è un
#: bypass dichiarato sull'asse RAG.
_ANY = "*"


@dataclass(frozen=True)
class RagMembership:
    """Indice `collection → membri`, più i seed che passano comunque.

    `by_collection` = {nome: {"read": [seed…], "write": [seed…]}}; le chiavi
    esistono solo dove qualcuno ha dichiarato qualcosa — la forma di UNA riga
    d'inventario la dà `of()`.
    """

    by_collection: dict[str, dict[str, list[str]]]
    bypass: list[str]

    def of(self, collection: Any) -> dict[str, list[str]]:
        """I campi member list di una riga d'inventario, sempre tutti e tre.

        Presenti anche quando sono vuoti, al contrario del `seeds:` di un
        datastore: lì il campo assente vuol dire «il manifest non si è
        pronunciato», qui la lista è CALCOLATA e «nessun seed la dichiara» è una
        risposta, non un'assenza di risposta.
        """
        per = self.by_collection.get(str(collection or ""), {})
        return {
            "seeds_read": list(per.get("read", ())),
            "seeds_write": list(per.get("write", ())),
            "seeds_bypass": list(self.bypass),
        }


def build(specs: Optional[Iterable] = None) -> RagMembership:
    """Gira i grant RAG di tutti i seed (default: la registry della colonia).

    Ordine stabile — i seed si scorrono per nome — perché queste liste finiscono
    a schermo: un ordine che cambia a ogni reload si legge come un cambio di
    autorizzazioni.
    """
    elenco = list(specs) if specs is not None else registry.list()
    per_nome = {str(getattr(s, "name", "") or ""): s for s in elenco}
    per_nome.pop("", None)
    by_collection: dict[str, dict[str, list[str]]] = {}
    bypass: list[str] = []
    for nome, spec in sorted(per_nome.items()):
        # Solo agenti ESEGUITI: un `human` non esegue verbi e un `proxy` non può
        # nemmeno dichiarare grant RAG (`models.py` rifiuta lo spec).
        if getattr(spec, "type", None) != "bot":
            continue
        assi = {
            "read": [str(c).strip() for c in (getattr(spec, "rag_read", None) or [])],
            "write": [str(c).strip() for c in (getattr(spec, "rag_write", None) or [])],
        }
        # SHORTCUT: un solo `bypass`, non uno per asse. Regge finché il bypass
        #           nasce da un grant che copre TUTTI i verbi RAG (lettura e
        #           scrittura insieme). Se un giorno servirà un seed che scrive
        #           ovunque ma legge solo una collection, qui vanno due liste.
        if _ANY in assi["read"] or _ANY in assi["write"] or _copre_i_verbi_rag(nome, per_nome):
            bypass.append(nome)
            continue
        for asse, collezioni in assi.items():
            for coll in collezioni:
                if not coll:
                    continue
                membri = by_collection.setdefault(coll, {}).setdefault(asse, [])
                if nome not in membri:
                    membri.append(nome)
    return RagMembership(by_collection, bypass)


def _copre_i_verbi_rag(nome: str, per_nome: dict) -> bool:
    """True se i verbi EFFETTIVI del seed (i propri più quelli ereditati)
    coprono l'intero namespace `rag.*`.

    Effettivi e non dichiarati: chi eredita da sysadmin ha `rag.*` davvero, e
    una member list che non lo vede promette un confinamento che non c'è. Un
    verbo PUNTUALE (`rag.collections`, che elenca i nomi) non è un bypass: se lo
    fosse, la lista diventerebbe «quasi tutti» e smetterebbe di dire qualcosa.
    """
    for grant in effective_tool_permissions(nome, per_nome):
        g = str(grant).strip()
        if not g or g.startswith("-"):  # negazione: toglie, non concede
            continue
        if _covers(g, _RAG_NS):
            return True
    return False
