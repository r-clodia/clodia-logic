"""Chi risponde in un canale: uno solo, e mai al posto di una persona.

Due regole date il 10 ago 2026, dopo aver visto il canale comportarsi male.

**Una risposta sola.** «a volte risponde più di un agent al messaggio utente,
invece deve essere solo uno». Il tetto esisteva già sul ramo dei tag e su quello
della delega — e non su quello del routing per rilevanza. Tre punti che
promettono la stessa cosa e uno che non la mantiene: è così che «risponde più di
un agente» sopravvive a un flag messo a OFF. Ora il tetto sta dove i turni
partono davvero.

Resta permesso l'unico caso che Davide ha ammesso: un agente che non può
soddisfare la richiesta ne menziona un altro. È un hop in sequenza, non un
fan-out — la seconda risposta arriva perché la prima l'ha chiesta.

**Una menzione a una persona non instrada un'AI.** `@matteo` non produceva
alcun target (`_pick_responder` restituisce solo agenti) e il codice cadeva nel
ramo «nessun tag → routing per rilevanza»: un agente rispondeva a una domanda
rivolta a un collega. Non era una regola mancante, era un buco — l'assenza di
un bersaglio letta come assenza di destinatario.

L'eccezione, sua: se il canale ha un gruppo Telegram collegato, il messaggero
prende un turno per dire che sta avvisando la persona di là. Non risponde nel
merito: porta fuori l'avviso e lo dichiara dentro, così chi resta nella stanza
sa che la palla è passata.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels as C


class _Spec:
    def __init__(self, name, tipo):
        self.name = name
        self.type = tipo


REGISTRO = {
    "davide": _Spec("davide", "human"),
    "matteo": _Spec("matteo", "human"),
    "clodia": _Spec("clodia", "super"),
    "messaggero": _Spec("messaggero", "normal"),
    "segretario": _Spec("segretario", "normal"),
}
PARTECIPANTI = list(REGISTRO)


def _reg(nome):
    return REGISTRO.get(nome)


class HumansAreNotRoutedTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(C.registry, "get_by_name", _reg)
        p.start()
        self.addCleanup(p.stop)

    def test_a_human_tag_is_recognised(self):
        self.assertEqual(C._humans_tagged("@matteo puoi guardare?", PARTECIPANTI),
                         ["matteo"])

    def test_an_agent_tag_is_not_a_human(self):
        self.assertEqual(C._humans_tagged("@segretario verbalizza", PARTECIPANTI), [])

    def test_a_soft_mention_of_a_human_counts_too(self):
        """`$matteo` è una citazione più leggera, ma resta rivolta a una
        persona: rispondere al posto suo sarebbe lo stesso errore."""
        self.assertEqual(C._humans_tagged("ne parlavo con $matteo", PARTECIPANTI),
                         ["matteo"])

    def test_someone_outside_the_channel_is_not_counted(self):
        """Un nome che non partecipa non è un destinatario di questa stanza."""
        self.assertEqual(C._humans_tagged("@giovanni ci sei?", PARTECIPANTI), [])

    def test_a_quoted_line_does_not_mention_anyone(self):
        """Le righe citate non producono menzioni: altrimenti rispondere a un
        messaggio che cita `@matteo` fermerebbe il canale per sbaglio."""
        self.assertEqual(C._humans_tagged("> @matteo aveva detto di sì\nio direi ok",
                                          PARTECIPANTI), [])

    def test_both_a_human_and_an_agent_still_stops_the_ai(self):
        """Il caso ambiguo, deciso dalla parte prudente: se nel messaggio c'è
        anche una persona, la richiesta la si considera sua."""
        self.assertEqual(C._humans_tagged("@matteo e @segretario, che dite?",
                                          PARTECIPANTI), ["matteo"])


class TelegramExceptionTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(C.registry, "get_by_name", _reg)
        p.start()
        self.addCleanup(p.stop)

    def test_the_notifier_is_named_not_guessed(self):
        """Sceglierlo per capacità («il primo che può inviare») farebbe
        dipendere da come sono configurati gli altri chi parla a nome della
        stanza."""
        self.assertEqual(C.TELEGRAM_NOTIFIER, "messaggero")

    def test_a_bound_group_is_seen(self):
        meta = {"meta": {"mounts": [
            {"name": "drive", "type": "drive", "config": {}},
            {"name": "telegram", "type": "telegram", "config": {"chat_id": "-1"}}]}}
        with patch.object(C.topics_client, "open_topic", lambda t, n: meta):
            self.assertTrue(C._telegram_group_bound("SEAL-1", "acme"))

    def test_other_mounts_are_not_a_telegram_group(self):
        meta = {"meta": {"mounts": [{"name": "drive", "type": "drive", "config": {}}]}}
        with patch.object(C.topics_client, "open_topic", lambda t, n: meta):
            self.assertFalse(C._telegram_group_bound("SEAL-1", "acme"))

    def test_an_unreadable_topic_means_no_group(self):
        """Fail-closed: nel dubbio non si prende il turno. L'esito è che nessuno
        risponde — che è la regola generale — invece di far parlare il
        messaggero su un canale che forse non ha un gruppo."""
        def rotto(t, n):
            raise RuntimeError("gateway giù")

        with patch.object(C.topics_client, "open_topic", rotto):
            self.assertFalse(C._telegram_group_bound("SEAL-1", "acme"))


class SingleAnswerTests(unittest.TestCase):
    def test_the_fan_out_is_off_by_default(self):
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHANNEL_MULTI_RESPONDER", None)
            self.assertFalse(C._multi_responder_enabled())

    def test_the_relevance_branch_now_caps_the_plan_too(self):
        """Il ramo che mancava. Gli altri due — tag e delega — lo facevano già;
        questo no, ed è quello che parte quando l'utente scrive senza taggare
        nessuno, cioè il caso più frequente."""
        import inspect
        src = inspect.getsource(C.post_channel_message)
        dopo_routing = src.split("nessun tag → routing per rilevanza")[-1]
        self.assertIn("plan = plan[:1]", dopo_routing)

    def test_a_delegation_hop_is_still_allowed(self):
        """L'unica pluralità ammessa: un agente che non può soddisfare la
        richiesta ne menziona un altro. È in sequenza — la seconda risposta
        arriva perché la prima l'ha chiesta — non in parallelo."""
        import inspect
        self.assertIn("delego solo a @", inspect.getsource(C))


if __name__ == "__main__":
    unittest.main()
