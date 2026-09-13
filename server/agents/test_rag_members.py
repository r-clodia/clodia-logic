"""Member list di una collection RAG: l'indice INVERSO dei grant dei seed.

`clodia-platform#341` chiede clearance + lista di seed autorizzati per ogni
datastore **o collection**. Per le collection il `tier` esisteva già proiettato;
la member list sembrava non esistere, ma esiste — girata dall'altra parte: è
`rag_read`/`rag_write` nel seed. Questi test fissano il verso in cui la si
legge, e soprattutto la differenza fra chi è MEMBRO di una collection e chi la
raggiunge comunque perché ha il namespace intero.
"""
from __future__ import annotations

import unittest

from .models import AgentSpec
from . import rag_members


def _spec(name: str, **campi) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "display_name": name.capitalize(), "description": "test",
        "type": campi.pop("type", "bot"), "system_prompt": "system-prompt.md",
        **campi,
    })


class RagMemberIndexTests(unittest.TestCase):

    def test_a_seed_declaring_rag_read_is_a_member_of_that_collection(self) -> None:
        idx = rag_members.build([_spec("aitiero", rag_read=["eu-normativa"])])
        self.assertEqual(["aitiero"], idx.of("eu-normativa")["seeds_read"])
        self.assertEqual([], idx.of("eu-normativa")["seeds_write"])

    def test_read_and_write_are_two_distinct_lists(self) -> None:
        """Chi ingesta non è chi consulta: il gate a valle deve poterli
        distinguere, quindi la proiezione non li fonde."""
        idx = rag_members.build([
            _spec("aitiero", rag_read=["prassi-fiscale"]),
            _spec("archivista", rag_read=["prassi-fiscale"],
                  rag_write=["prassi-fiscale"]),
        ])
        riga = idx.of("prassi-fiscale")
        self.assertEqual(["aitiero", "archivista"], riga["seeds_read"])
        self.assertEqual(["archivista"], riga["seeds_write"])

    def test_a_collection_nobody_declares_answers_with_empty_lists(self) -> None:
        """La lista è CALCOLATA: «nessun seed la dichiara» è una risposta, e va
        detta con una lista vuota — non con un campo assente, che nella pagina
        Databases significa «il manifest non si è pronunciato»."""
        riga = rag_members.build([_spec("aitiero", rag_read=["eu-normativa"])]).of("orfana")
        self.assertEqual({"seeds_read": [], "seeds_write": [], "seeds_bypass": []}, riga)

    def test_a_whole_namespace_grant_is_bypass_not_a_member_everywhere(self) -> None:
        """`rag.*` (sysadmin, il provisioner dei pack) raggiunge QUALUNQUE
        collection senza nominarne nessuna. Ripeterlo in ogni riga direbbe due
        falsità: che è membro di quella collection, e che la member list lo
        governa."""
        idx = rag_members.build([
            _spec("sysadmin", tool_permissions=["rag.*"]),
            _spec("aitiero", rag_read=["eu-normativa"]),
        ])
        riga = idx.of("eu-normativa")
        self.assertEqual(["aitiero"], riga["seeds_read"])
        self.assertEqual(["sysadmin"], riga["seeds_bypass"])
        # e il bypass si vede anche su una collection che nessuno dichiara
        self.assertEqual(["sysadmin"], idx.of("orfana")["seeds_bypass"])

    def test_a_pointwise_rag_verb_is_not_a_bypass(self) -> None:
        """`rag.collections` (clodia) elenca, non apre il corpus: chi ha un
        verbo puntuale non copre il namespace e non finisce fra i bypass —
        altrimenti la lista diventerebbe «quasi tutti» e smetterebbe di dire
        qualcosa."""
        idx = rag_members.build([_spec("clodia", tool_permissions=["rag.collections"])])
        self.assertEqual([], idx.of("eu-normativa")["seeds_bypass"])

    def test_a_star_in_rag_read_is_a_bypass_not_a_collection_named_star(self) -> None:
        idx = rag_members.build([_spec("tuttologo", rag_read=["*"])])
        self.assertEqual(["tuttologo"], idx.of("qualunque")["seeds_bypass"])
        self.assertEqual({}, idx.by_collection)

    def test_an_inherited_namespace_grant_counts(self) -> None:
        """I verbi effettivi, non quelli scritti nel file: un seed che eredita
        `rag.*` lo ha davvero (`inheritance.effective_tool_permissions`)."""
        idx = rag_members.build([
            _spec("sysadmin", tool_permissions=["rag.*"]),
            _spec("vice", parents=["sysadmin"], tool_permissions=[]),
        ])
        self.assertEqual(["sysadmin", "vice"], idx.of("eu-normativa")["seeds_bypass"])

    def test_a_negated_grant_does_not_open_the_namespace(self) -> None:
        idx = rag_members.build([_spec("tagliato", tool_permissions=["-rag.*"])])
        self.assertEqual([], idx.of("eu-normativa")["seeds_bypass"])

    def test_a_human_principal_is_not_a_member(self) -> None:
        """Un `human` non esegue verbi (non ha runtime): il suo nome in una
        member list sarebbe rumore che nessun gate leggerà mai."""
        idx = rag_members.build([_spec("davide", type="human", rag_read=["eu-normativa"])])
        self.assertEqual([], idx.of("eu-normativa")["seeds_read"])

    def test_members_are_stable_and_deduplicated(self) -> None:
        """La pagina Databases mostra queste liste: un ordine che balla a ogni
        reload si legge come un cambio di autorizzazioni."""
        idx = rag_members.build([
            _spec("zeta", rag_read=["c"]),
            _spec("alfa", rag_read=["c", "c"]),
        ])
        self.assertEqual(["alfa", "zeta"], idx.of("c")["seeds_read"])


if __name__ == "__main__":
    unittest.main()
