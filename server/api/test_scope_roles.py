"""Quale ruolo serve per quale endpoint.

La regola, dalla voce 25 e dalla 26: **leggere** è di chiunque sia nella stanza,
**parlare** anche — è il punto della definizione di reader — e si gradua solo ciò
che MUTA lo stato condiviso.

Prima del 7 ago 2026 l'appartenenza era binaria e dieci endpoint avevano la
stessa guardia. Un invitato in `proof-of-flex` poteva:

  - azzerare la memoria conversazionale del canale (`reset-context`)
  - caricare `AGENTS.md`, cioè il testo iniettato nel contesto di ogni agente a
    ogni turno
  - interrompere il turno di un agente

Nessuna delle tre è una lettura, e nessuna era protetta.

Nota sul reader: gli si ferma solo l'atto DIRETTO. La sua richiesta non viene
ignorata — la porta un agente, e se implica una mutazione diventa un gate rivolto
all'owner. Un rifiuto secco a una richiesta legittima sarebbe la risposta
sbagliata; il punto è che non passi *senza* valutazione.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from . import channels as C


OWNER = {"owner": "davide", "participants": {"clodia": "contributor",
                                             "matteo": "reader"}}
LEGACY = {"owner": "davide", "participants": ["clodia", "giovanni"]}


class _Req:
    def __init__(self, chi):
        self.chi = chi


def _as(chi):
    return patch.object(C, "_principal_from_request", lambda r: chi)


class RoleReadingTests(unittest.TestCase):
    def test_roles_are_read_from_the_map(self):
        self.assertEqual(C._scope_role(OWNER, "davide"), "owner")
        self.assertEqual(C._scope_role(OWNER, "clodia"), "contributor")
        self.assertEqual(C._scope_role(OWNER, "matteo"), "reader")

    def test_a_legacy_list_reads_as_contributors(self):
        """Il comportamento di ieri resta quello di oggi: reader sarebbe più
        stretto ma toglierebbe la parola a tutti in una volta."""
        self.assertEqual(C._scope_role(LEGACY, "clodia"), "contributor")
        self.assertEqual(C._scope_role(LEGACY, "giovanni"), "contributor")

    def test_someone_outside_has_no_role(self):
        self.assertIsNone(C._scope_role(OWNER, "estraneo"))

    def test_an_unknown_role_string_degrades_to_contributor(self):
        """Un valore che non capiamo non deve promuovere a owner né azzittire:
        vale l'appartenenza semplice."""
        m = {"owner": "davide", "participants": {"x": "capo"}}
        self.assertEqual(C._scope_role(m, "x"), "contributor")


class ReadingTests(unittest.TestCase):
    def test_every_role_may_read(self):
        """Essere vincolati da regole che non si possono vedere è il difetto
        peggiore disponibile."""
        for chi in ("davide", "clodia", "matteo"):
            with self.subTest(chi=chi), _as(chi):
                self.assertEqual(C._require_member(_Req(chi), OWNER), chi)

    def test_an_outsider_may_not_read(self):
        with _as("estraneo"):
            with self.assertRaises(HTTPException) as cm:
                C._require_member(_Req("estraneo"), OWNER)
            self.assertEqual(cm.exception.status_code, 403)


class MutationTests(unittest.TestCase):
    def test_owner_and_contributor_may_mutate(self):
        for chi in ("davide", "clodia"):
            with self.subTest(chi=chi), _as(chi):
                self.assertEqual(C._require_contributor(_Req(chi), OWNER), chi)

    def test_a_reader_may_not_mutate_directly(self):
        with _as("matteo"):
            with self.assertRaises(HTTPException) as cm:
                C._require_contributor(_Req("matteo"), OWNER)
            self.assertEqual(cm.exception.status_code, 403)

    def test_the_refusal_tells_the_reader_the_other_road(self):
        """Un rifiuto che non dice come procedere insegna solo che il sistema
        dice di no."""
        with _as("matteo"):
            with self.assertRaises(HTTPException) as cm:
                C._require_contributor(_Req("matteo"), OWNER)
            testo = str(cm.exception.detail)
            self.assertIn("owner", testo)
            self.assertIn("approvazione", testo)


class OwnershipTests(unittest.TestCase):
    def test_only_the_owner_may_act_as_owner(self):
        with _as("davide"):
            self.assertEqual(C._require_scope_owner(_Req("davide"), OWNER), "davide")
        for chi in ("clodia", "matteo"):
            with self.subTest(chi=chi), _as(chi):
                with self.assertRaises(HTTPException):
                    C._require_scope_owner(_Req(chi), OWNER)


class EndpointAssignmentTests(unittest.TestCase):
    """La mappa endpoint→guardia, letta dal sorgente. Se un endpoint mutante
    torna alla guardia di lettura, il difetto di ieri si ripresenta identico e
    senza che nulla fallisca."""

    def _guardie(self):
        import inspect
        import re
        src = inspect.getsource(C).splitlines()
        rotta, out = None, {}
        for l in src:
            m = re.match(r'@router\.(get|post|delete|put)\("([^"]+)"', l.strip())
            if m:
                rotta = (m.group(1), m.group(2))
            for g in ("_require_scope_owner(request", "_require_contributor(request",
                      "_require_member(request"):
                if g in l and rotta:
                    out[rotta] = g.split("(")[0]
                    rotta = None
                    break
        return out

    def test_destroying_shared_state_is_an_act_of_ownership(self):
        """`reset-context` azzera la memoria conversazionale di TUTTI i
        partecipanti: somiglia più alla proprietà che alla partecipazione."""
        g = self._guardie()
        self.assertEqual(g.get(("post", "/clodia/channels/{tier}/{name}/reset-context")),
                         "_require_scope_owner")

    def test_moving_the_walls_is_an_act_of_ownership(self):
        g = self._guardie()
        self.assertEqual(g.get(("post", "/clodia/channels/{tier}/{name}/remote")),
                         "_require_scope_owner")

    def test_interrupting_a_turn_is_a_mutation(self):
        g = self._guardie()
        self.assertEqual(g.get(("post", "/clodia/channels/{tier}/{name}/interrupt")),
                         "_require_contributor")

    def test_feedback_is_a_mutation_because_it_becomes_a_lesson(self):
        """Il feedback diventa una lesson nel prompt dell'agente: scrive in ciò
        che l'agente legge a ogni turno."""
        g = self._guardie()
        self.assertEqual(
            g.get(("post", "/clodia/channels/{tier}/{name}/messages/{message_id}/feedback")),
            "_require_contributor")

    def test_reading_endpoints_stay_open_to_every_member(self):
        g = self._guardie()
        for r in (("get", "/clodia/channels/{tier}/{name}/messages"),
                  ("get", "/clodia/channels/{tier}/{name}/files"),
                  ("get", "/clodia/channels/{tier}/{name}/agents-md")):
            with self.subTest(rotta=r):
                self.assertEqual(g.get(r), "_require_member")

    def test_uploading_is_guarded_by_a_function_not_by_hand(self):
        """Il controllo sull'upload era scritto a mano invece di passare da una
        guardia, ed è per questo che era rimasto indietro. Una regola duplicata
        diverge — è la stessa lezione del confronto `== "admin"`."""
        import inspect
        src = inspect.getsource(C.channel_upload)
        self.assertIn("_require_contributor", src)
        self.assertNotIn('not in meta.get("participants"', src)


if __name__ == "__main__":
    unittest.main()
