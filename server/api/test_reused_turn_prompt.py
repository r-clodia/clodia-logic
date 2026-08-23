"""Un agente deve riconoscere i PROPRI messaggi in canale.

    «Ottima cattura sulla collisione — […] Procedi coi punti 1–6.»
                        — lo stesso messaggio, recapitato cinque volte, 23 ago 2026

Misurato in `software-house`: un ordine già eseguito tornava identico a ogni
riconsegna, e lo spawn non aveva modo di sapere di averlo già servito — al punto
che su clodia-platform#268 è stato aperto un secondo spawn su un lavoro già
consegnato, con due PR gemelle sullo stesso file.

La causa non è il router: è che `_reused_turn_prompt` cercava l'ultimo messaggio
proprio confrontando `author == responder` LETTERALMENTE. In canale i messaggi
sono firmati dallo spawn (`fullstack-dev-86`), mentre `responder` arriva anche
come nome del seed (`fullstack-dev`), e i due non combaciano mai:

    last_own = -1  →  tutta la storia è «non vista»  →  storico intero ogni turno

Il prezzo non è solo il contesto sprecato: senza un confine fra «ciò che ho già
visto» e «ciò che è nuovo», nessun turno può distinguere un ordine nuovo dalla
ripetizione di uno vecchio.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


FALLBACK = "FALLBACK: solo il messaggio nuovo"


class OwnMessagesAreRecognisedBySeedTests(unittest.TestCase):
    def setUp(self) -> None:
        # `_seed_name` taglia `-N` solo se il prefisso è un seed registrato:
        # senza registry, `fullstack-dev-86` resterebbe intero.
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: (
            object() if n in ("fullstack-dev", "clodia") else None)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig

    def _prompt(self, msgs: list[dict], responder: str = "fullstack-dev") -> str:
        with patch.object(channels.topics_client, "list_messages",
                          return_value=msgs):
            return channels._reused_turn_prompt(
                "SEAL-1", "software-house", responder, "davide", FALLBACK)

    def test_a_spawn_recognises_its_own_previous_message(self) -> None:
        """Il difetto. L'unico messaggio nuovo è quello di davide: lo storico
        intero non serve, e ripresentarlo rimette in circolo ordini già serviti."""
        prompt = self._prompt([
            {"author": "davide", "kind": "human", "text": "lavora la 268"},
            {"author": "fullstack-dev-86", "kind": "ai", "text": "fatto, PR #346"},
            {"author": "davide", "kind": "human", "text": "messaggio nuovo"},
        ])

        self.assertEqual(FALLBACK, prompt)

    def test_the_seed_form_keeps_working(self) -> None:
        """L'altro chiamante passa il nome del seed: non deve regredire."""
        prompt = self._prompt([
            {"author": "fullstack-dev", "kind": "ai", "text": "fatto"},
            {"author": "davide", "kind": "human", "text": "messaggio nuovo"},
        ])

        self.assertEqual(FALLBACK, prompt)

    def test_the_label_form_of_the_responder_also_works(self) -> None:
        """`_deliver_to_session` passa l'ETICHETTA, non il seed: entrambi i lati
        vanno normalizzati, o il confronto fallisce dall'altro verso."""
        prompt = self._prompt([
            {"author": "fullstack-dev-86", "kind": "ai", "text": "fatto"},
            {"author": "davide", "kind": "human", "text": "messaggio nuovo"},
        ], responder="fullstack-dev-86")

        self.assertEqual(FALLBACK, prompt)

    def test_a_third_party_message_is_still_handed_over(self) -> None:
        """Il fix non deve diventare cecità: ciò che ha detto un ALTRO agente
        dall'ultimo turno resta la ragione per cui questa funzione esiste."""
        prompt = self._prompt([
            {"author": "fullstack-dev-86", "kind": "ai", "text": "fatto"},
            {"author": "clodia-64", "kind": "ai", "text": "chiudi la #347"},
            {"author": "davide", "kind": "human", "text": "messaggio nuovo"},
        ])

        self.assertNotEqual(FALLBACK, prompt)
        self.assertIn("chiudi la #347", prompt)

    def test_a_spawn_of_the_same_seed_is_not_a_third_party(self) -> None:
        """Due spawn dello stesso seed sono lo stesso interlocutore per questo
        confronto: è la stessa normalizzazione, applicata all'altro spawn."""
        prompt = self._prompt([
            {"author": "fullstack-dev-86", "kind": "ai", "text": "fatto"},
            {"author": "davide", "kind": "human", "text": "messaggio nuovo"},
        ], responder="fullstack-dev-88")

        self.assertEqual(FALLBACK, prompt)


if __name__ == "__main__":
    unittest.main()
