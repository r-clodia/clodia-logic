"""Il progressivo di spawn si vede SEMPRE: in chat, nel live e nei log.

Requisito di Davide (7 ago 2026, ribadito il 18): «ogni spawn ha un progressivo
per seed e deve essere sempre mostrato a schermo e nei log». Il numero è quello
della directory dello spawn — `clodia-124` — progressivo per seed, persistito e
mai riusato (system-notebook 7); NON l'ordinale di canale `#N`, che è relativo,
capped e riusabile.

Era una regressione, e il modo in cui è passata è la parte che vale:

    _spawn_label leggeva chat._spawn_dir
    ChatSession (runtime Claude) tiene la dir in self._spawn e passa
        `spawn_dir` come VARIABILE LOCALE
    CodexChatSession / OpenCodeChatSession tengono anche self._spawn_dir

Quindi sul runtime Claude — clodia, ophelia, fullstack-dev — `_spawn_label` non
trovava nulla e ripiegava sul nome del seed: nessun numero, mai. I test esistenti
non lo vedevano perché esercitano `_spawn_label` su una Chat **finta** che
l'attributo lo ha, e asseriscono il fallback come comportamento voluto. Un test
che verifica il contratto su un doppio, mentre l'oggetto vero non lo rispetta,
dice verde su un requisito inerte.

La lezione, e il motivo per cui questi test guardano le classi VERE: quando due
letture della stessa verità vivono in due posti (`_spawn_label` e
`live_spawn_dirs`), una resta indietro. Ora leggono entrambe `spawn_dirs_of`.
"""
from __future__ import annotations

import inspect
import pathlib
import unittest
from unittest.mock import patch

from . import channels as C
from ..sdk_runtime import session as S


class TheRealSessionsExposeTheirSpawnTests(unittest.TestCase):
    """Il buco che ha lasciato passare la regressione: nessuno guardava le
    classi vere. Istanziarle richiede un provider e un subprocess, quindi si
    ispeziona il sorgente — è la stessa tecnica di
    `test_working_is_not_stuck`, per la stessa ragione."""

    RUNTIME = ("ChatSession", "CodexChatSession", "OpenCodeChatSession")

    def test_every_runtime_keeps_the_spawn_where_the_label_looks(self) -> None:
        for nome in self.RUNTIME:
            with self.subTest(runtime=nome):
                src = inspect.getsource(getattr(S, nome))
                self.assertRegex(
                    src, r"self\._spawn(_dir)?\s*[,=]",
                    f"{nome} non conserva lo spawn: `_spawn_label` ripiegherebbe "
                    f"sul nome del seed e il progressivo non comparirebbe")

    def test_the_label_and_the_reaper_read_the_same_thing(self) -> None:
        """Una lettura sola. Con due, quella dimenticata è esattamente il difetto
        del 18 ago: il reaper vedeva ogni spawn (leggeva entrambi gli attributi),
        l'etichetta no (ne leggeva uno)."""
        self.assertIn("spawn_dirs_of", inspect.getsource(C._spawn_label))
        self.assertIn("spawn_dirs_of", inspect.getsource(S.ChatManager.live_spawn_dirs))

    def test_the_claude_runtime_shape_is_covered(self) -> None:
        """Il caso concreto che era rotto: una sessione che tiene la dir SOLO in
        `_spawn` (com'è `ChatSession`) deve dare il nome dello spawn."""
        class SoloSpawn:
            class _S:
                dir = pathlib.Path("/datadir/spawns/clodia-124")
            _spawn = _S()
        self.assertEqual("clodia-124", C._spawn_label(SoloSpawn(), "clodia"))

    def test_the_other_runtimes_shape_still_works(self) -> None:
        class SoloSpawnDir:
            _spawn_dir = pathlib.Path("/datadir/spawns/messaggero-9")
        self.assertEqual("messaggero-9", C._spawn_label(SoloSpawnDir(), "messaggero"))

    def test_spawn_dirs_of_survives_a_broken_object(self) -> None:
        class Rotta:
            @property
            def _spawn(self):
                raise RuntimeError("boom")

            @property
            def _spawn_dir(self):
                raise RuntimeError("boom")
        self.assertEqual([], S.spawn_dirs_of(Rotta()))
        self.assertEqual("clodia", C._spawn_label(Rotta(), "clodia"))


class TheNumberIsAbsoluteNotRelativeTests(unittest.TestCase):
    """`#N` è l'ordinale di CANALE: relativo, capped a `max_spawns`, riusato
    appena il reaper evince un'istanza. `-N` è il numero dello spawn. Mostrare il
    primo al posto del secondo significa mostrare un numero che non identifica
    nulla: `fullstack-dev#2` può essere `-7` su disco, e domani `-11`."""

    def test_the_directive_tells_the_instance_its_spawn_number(self) -> None:
        """Le si diceva «sei nome#2» e «i tuoi messaggi appaiono come nome#2».
        La seconda frase era falsa — l'autore del messaggio è il nome dello
        spawn — quindi le si insegnava a firmarsi con un numero che nessun altro
        le attribuiva."""
        src = inspect.getsource(C._start_turn)
        self.assertIn("spawn_nome", src)
        self.assertNotIn('f"[Sei l\'istanza {label}', src,
                         "la direttiva usa ancora l'ordinale di canale")

    def test_the_audit_line_carries_the_spawn_not_the_ordinal(self) -> None:
        """«e nei log». Una riga d'audit che dice `#2` non dice quale processo:
        due carichi diversi in momenti diversi hanno lo stesso `#2`."""
        src = inspect.getsource(C._start_turn)
        self.assertIn('"instance": _spawn_label(chat, spec.name)', src)

    def test_a_single_instance_seed_is_told_its_number_too(self) -> None:
        """«tutti gli spawn». Un seed non multi-spawn gira comunque come
        `nome-N`: se non glielo si dice si firma col nome del seed mentre il
        canale mostra il numero, e chi legge vede due nomi per un interlocutore."""
        src = inspect.getsource(C._start_turn)
        self.assertIn("elif spawn_nome != spec.name:", src)

    def test_the_live_events_carry_the_spawn_name(self) -> None:
        """Il numero si vede anche MENTRE l'agente lavora. Gli eventi live
        portano il `chat_id`, da cui la webui ricavava `seed#N` (o `seed` nudo):
        durante il turno un nome, a turno finito un altro."""
        src = inspect.getsource(C._start_turn)
        self.assertIn('type="spawn_label"', src)
        self.assertIn('"spawn": spawn_nome', src)


class TheHistoricalFormIsStillUnderstoodTests(unittest.TestCase):
    """Smettere di CAPIRE `#N` non è nei requisiti: sta scritto nei messaggi già
    inviati e nella memoria degli agenti. Cambia ciò che si MOSTRA."""

    def setUp(self) -> None:
        noti = {"fullstack-dev", "clodia"}
        self.p = patch.object(C.registry, "get_by_name",
                              lambda n: object() if n in noti else None)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_both_forms_still_parse(self) -> None:
        self.assertEqual(C._split_ord("fullstack-dev#1"), ("fullstack-dev", 1))
        self.assertEqual(C._split_ord("fullstack-dev-2"), ("fullstack-dev", 2))

    def test_the_seed_is_still_recovered(self) -> None:
        self.assertEqual("fullstack-dev", C._seed_name("fullstack-dev-124"))
        self.assertEqual("clodia", C._seed_name("clodia#3"))


if __name__ == "__main__":
    unittest.main()
