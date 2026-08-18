"""Un proxy ha un pallino di presenza, come una persona. Un bot no.

Richiesta di Davide, 18 ago 2026: «i proxy, così come gli umani, dovrebbero avere
un pallino che segnala la loro presenza».

Il meccanismo non ha avuto bisogno di nulla, e vale la pena dirlo: `presence.touch`
scrive per QUALUNQUE membro che legge i messaggi del canale — l'identità viene da
`_require_member`, non da un controllo di tipo. Quindi la presenza di un proxy era
**già registrata** da quando i proxy esistono. Non veniva mostrata perché il filtro
di visualizzazione chiedeva `type == "human"`: un dato raccolto e mai letto.

Il confine che questi test difendono è l'altro: i `bot` restano fuori. Su un agente
lo stesso pallino risponderebbe a una domanda diversa — è vivo? sta lavorando? — e
per quella ci sono `active_responders` e il box live. Un simbolo che significa due
cose non ne significa nessuna.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import channels as ch


def _spec(tipo: str):
    return SimpleNamespace(type=tipo)


REGISTRY = {
    "davide": _spec("human"),
    "giovanni": _spec("human"),
    "clodia-primal": _spec("proxy"),
    "clodia": _spec("bot"),
    "fullstack-dev": _spec("bot"),
    "ophelia": _spec("super"),
}


class ChiHaUnPallinoTests(unittest.TestCase):
    def setUp(self) -> None:
        # Si patcha l'oggetto REGISTRY vero e non `channels.registry`: la
        # funzione fa `from ..agents import registry` DENTRO il corpo (import
        # locale, presumibilmente per un ciclo), quindi rilega il nome a ogni
        # chiamata e un patch sul modulo chiamante non la raggiunge. Un test che
        # patchava il posto sbagliato passava di verde su un filtro mai eseguito.
        from ..agents import registry as vero
        p = patch.object(vero, "get_by_name", lambda n: REGISTRY.get(n))
        p.start()
        self.addCleanup(p.stop)

    def _con(self, participants, owner="davide"):
        return ch._partecipanti_con_presenza({"participants": participants,
                                              "owner": owner})

    def test_a_proxy_is_included(self) -> None:
        """Il difetto: era escluso dal filtro pur avendo la presenza registrata."""
        self.assertIn("clodia-primal", self._con(["davide", "clodia-primal"]))

    def test_a_human_is_still_included(self) -> None:
        self.assertIn("davide", self._con(["davide", "clodia-primal"]))

    def test_a_bot_is_not(self) -> None:
        """Il confine da non perdere: su un agente lo stesso pallino
        risponderebbe a «sta lavorando?», che è un'altra domanda."""
        fuori = self._con(["davide", "clodia", "fullstack-dev", "ophelia"])
        for bot in ("clodia", "fullstack-dev", "ophelia"):
            self.assertNotIn(bot, fuori)

    def test_an_unknown_participant_is_not_included(self) -> None:
        """Chi il registry non conosce non ha un tipo, e senza tipo non si
        inventa una presenza."""
        self.assertNotIn("sconosciuto", self._con(["davide", "sconosciuto"]))

    def test_the_owner_is_included_even_if_not_listed(self) -> None:
        """Comportamento preesistente, conservato: l'owner ha un posto anche
        quando non compare fra i participants."""
        self.assertIn("davide", self._con(["clodia"], owner="davide"))

    def test_a_proxy_owner_is_included_too(self) -> None:
        self.assertIn("clodia-primal", self._con(["clodia"], owner="clodia-primal"))

    def test_no_duplicates_when_owner_is_also_a_participant(self) -> None:
        out = self._con(["davide", "clodia-primal", "davide"], owner="davide")
        self.assertEqual(len(out), len(set(out)))

    def test_the_declared_types_are_the_two_with_someone_behind_them(self) -> None:
        """La lista è dichiarata in un posto solo: se un giorno si aggiunge un
        tipo, si aggiunge lì e non in un `or` sparso per il modulo."""
        self.assertEqual(("human", "proxy"), ch._TIPI_CON_PRESENZA)


class IlBattitoNonGuardaIlTipoTests(unittest.TestCase):
    """La ragione per cui questa modifica è di sole tre righe.

    Se `touch` filtrasse per tipo, un proxy non avrebbe avuto nessuna presenza da
    mostrare e servirebbe un'altra sorgente. Non filtra, e non deve iniziare: la
    domanda «chi ha letto questa stanza» è la stessa per tutti.
    """

    def test_touch_writes_for_any_principal(self) -> None:
        import inspect
        from . import presence
        src = inspect.getsource(presence.touch)
        self.assertNotIn("type", src,
                         "touch ha iniziato a guardare il tipo: la presenza di un "
                         "proxy smetterebbe di essere registrata")


if __name__ == "__main__":
    unittest.main()
