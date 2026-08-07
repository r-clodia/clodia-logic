"""In chat parla lo SPAWN, non il seed.

Regola di Davide, 7 ago 2026: «nelle chat devono apparire i nomi degli spawn e
non dei seed. Quindi clodia-4 parla e non clodia». E, subito dopo: «l'ordinale di
canale sparisce, d'ora in poi si usa ordinale globale per seed».

Perché contava. Ce n'erano DUE per la stessa cosa:

    directory degli spawn   clodia-1 · fullstack-dev-2 · fullstack-dev-3
    etichetta in chat       fullstack-dev#1

`#N` era un ordinale per CANALE — con un cap, e riusabile. `-N` è il numero dello
spawn, progressivo per seed e mai riusato (system-notebook 7). Quello mostrato era
il primo: `fullstack-dev#1` in chat poteva essere `-2` o `-3` su disco, quindi il
nome non identificava l'istanza con cui stavi parlando.

Il taglio su `-N` è ambiguo di suo, perché i nomi dei seed contengono trattini:
`security-engineer-1` va tagliato dopo `engineer`. Si taglia solo se il prefisso è
un seed che esiste — altrimenti un agente chiamato `tomato-2` diventerebbe
l'istanza 2 di un `tomato` inesistente.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels as C


class _Spec:
    pass


SEED_NOTI = {"fullstack-dev", "security-engineer", "clodia", "tomato", "messaggero"}


def _registry(nome):
    return _Spec() if nome in SEED_NOTI else None


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.p = patch.object(C.registry, "get_by_name", _registry)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_a_spawn_name_splits_into_seed_and_number(self):
        self.assertEqual(C._split_ord("fullstack-dev-2"), ("fullstack-dev", 2))

    def test_a_seed_with_dashes_splits_at_the_right_place(self):
        """`security-engineer-1` va tagliato dopo `engineer`, non dopo
        `security`."""
        self.assertEqual(C._split_ord("security-engineer-1"), ("security-engineer", 1))

    def test_a_bare_seed_has_no_number(self):
        self.assertEqual(C._split_ord("clodia"), ("clodia", None))

    def test_an_unknown_name_is_left_whole(self):
        """La direzione che non inventa istanze: se il prefisso non è un seed,
        l'etichetta resta com'è."""
        self.assertEqual(C._split_ord("sconosciuto-9"), ("sconosciuto-9", None))

    def test_the_historical_form_is_still_understood(self):
        """`#N` sta scritto nei messaggi già inviati e nella memoria degli
        agenti: smettere di capirlo trasformerebbe una menzione storica in un tag
        che non risolve."""
        self.assertEqual(C._split_ord("fullstack-dev#1"), ("fullstack-dev", 1))

    def test_the_seed_is_recovered_from_a_spawn_label(self):
        """Serve ovunque si confronti un autore con i partecipanti: se il seed
        non si ricavasse più dall'etichetta, un'istanza smetterebbe di risultare
        partecipante del proprio canale."""
        self.assertEqual(C._seed_name("fullstack-dev-2"), "fullstack-dev")
        self.assertEqual(C._seed_name("clodia"), "clodia")


class LabelTests(unittest.TestCase):
    def test_the_author_is_the_spawn_directory_name(self):
        import pathlib

        class Chat:
            _spawn_dir = pathlib.Path("/datadir/spawns/clodia-4")
        self.assertEqual(C._spawn_label(Chat(), "clodia"), "clodia-4")

    def test_without_a_materialised_spawn_it_falls_back_to_the_seed(self):
        """Meglio un nome meno preciso che nessun autore: un'etichetta non deve
        poter rompere un turno."""
        class Chat:
            _spawn_dir = None
        self.assertEqual(C._spawn_label(Chat(), "clodia"), "clodia")
        self.assertEqual(C._spawn_label(object(), "clodia"), "clodia")

    def test_a_broken_chat_object_does_not_raise(self):
        class Chat:
            @property
            def _spawn_dir(self):
                raise RuntimeError("boom")
        self.assertEqual(C._spawn_label(Chat(), "messaggero"), "messaggero")


if __name__ == "__main__":
    unittest.main()
