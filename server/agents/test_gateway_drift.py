"""La dichiarazione di un seed che non è arrivata al gateway si deve VEDERE.

`register_agent` aveva un chiamante solo — l'import di un pack — quindi
`tool_permissions`, `gated_tools`, `profile_tools` e `denied_tools`
raggiungevano l'autorità una volta sola. Da lì in poi la config del gateway è la
fotografia di quel giorno, e il seed una dichiarazione che nessuno rilegge
(clodia-platform#203).

I tre guasti che hanno prodotto l'issue sono tutti e tre *silenziosi*: quattro
giorni di 500 sulla rotta di registrazione senza che nessuno se ne accorgesse,
`ophelia` con un `['*']` sopravvissuto al proprio ritiro, e un confinamento che
non è cambiato finché non è stato riscritto a mano su due istanze. Una
divergenza che si annuncia li avrebbe colti tutti.

Qui si misura che si annunci — e che NON si risolva da sé, tranne dove non c'è
niente da proteggere.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from . import gateway_drift as GD


def _seed(**kw):
    base = dict(name="avvocato", type="normal", tool_permissions=["topic.open"],
                gated_tools=None, denied_tools=None, profile_tools=None,
                gated_in_channel=None, carries=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _reg(**kw):
    base = dict(agent="avvocato", registered=True, allowed_tools=["topic.open"],
                gated_tools=[], denied_tools=[], profile_tools=[])
    base.update(kw)
    return base


class ConfrontoTests(unittest.TestCase):
    def test_aligned_is_clean(self):
        self.assertEqual(GD.confronta(_seed(), _reg())["status"], "clean")

    def test_a_declaration_that_never_arrived_is_missing(self):
        """Il caso #277: il seed dichiara un confinamento, il gateway non ne sa
        niente, e per giorni non è cambiato niente."""
        riga = GD.confronta(_seed(denied_tools=["email.send"]), _reg())
        self.assertEqual(riga["status"], "diverged")
        campo = riga["fields"][0]
        self.assertEqual(campo["field"], "denied_tools")
        self.assertEqual(campo["status"], "missing")
        self.assertEqual(campo["declared_only"], ["email.send"])

    def test_something_only_in_the_gateway_is_extra(self):
        """Il caso `ophelia`: un `*` che il seed non dichiara più e che nella
        config viva è rimasto. Si segnala e NON si toglie — potrebbe essere una
        modifica intenzionale, ed è il verso in cui l'overwrite fa danno."""
        riga = GD.confronta(_seed(tool_permissions=[]), _reg(allowed_tools=["*"]))
        campo = riga["fields"][0]
        self.assertEqual(campo["status"], "extra")
        self.assertEqual(campo["registered_only"], ["*"])

    def test_a_changed_list_says_what_entered_and_what_left(self):
        """Su liste di venti verbi l'elenco intero è rumore: le due righe utili
        sono cosa è entrato e cosa è uscito."""
        riga = GD.confronta(_seed(tool_permissions=["topic.open", "email.send"]),
                            _reg(allowed_tools=["topic.open", "fs.read"]))
        campo = riga["fields"][0]
        self.assertEqual(campo["status"], "changed")
        self.assertEqual(campo["declared_only"], ["email.send"])
        self.assertEqual(campo["registered_only"], ["fs.read"])

    def test_reordering_is_not_drift(self):
        riga = GD.confronta(_seed(tool_permissions=["b", "a"]),
                            _reg(allowed_tools=["a", "b"]))
        self.assertEqual(riga["status"], "clean")

    def test_none_and_empty_are_the_same_thing(self):
        """«Il seed non si pronuncia» e «lista vuota» si comportano identici
        all'enforcement (`spec.get(campo) or []`): segnalarli come divergenza
        significherebbe segnalare ogni agente, sempre."""
        self.assertEqual(GD.confronta(_seed(gated_tools=None), _reg(gated_tools=[]))["status"],
                         "clean")

    def test_untransported_fields_are_not_compared(self):
        """`carries` e `gated_in_channel` viaggiano con la registrazione ma il
        gateway non li conserva: confrontarli darebbe una divergenza permanente
        e falsa su OGNI agente — un rilevatore che nessuno rilegge."""
        campi = [seed for seed, _gw in GD.CAMPI]
        self.assertNotIn("carries", campi)
        self.assertNotIn("gated_in_channel", campi)
        riga = GD.confronta(_seed(carries=["x"], gated_in_channel=["email.send"]), _reg())
        self.assertEqual(riga["status"], "clean")

    def test_an_unreadable_gateway_is_not_clean(self):
        """Dire «nessuna divergenza» perché non si è potuto guardare è la bugia
        esatta che questa riconciliazione esiste per togliere."""
        riga = GD.confronta(_seed(), None)
        self.assertEqual(riga["status"], "unavailable")
        self.assertNotEqual(riga["status"], "clean")

    def test_an_agent_absent_from_the_config_is_unregistered(self):
        riga = GD.confronta(_seed(), {"agent": "avvocato", "registered": False})
        self.assertEqual(riga["status"], "unregistered")


class CheckAllTests(unittest.TestCase):
    """Il giro completo: chi si guarda, cosa si ripara, cosa no."""

    def _run(self, specs, registrazioni, repair=True):
        from ..api import gateway_admin
        registrati: list = []

        def _fetch(nome):
            r = registrazioni.get(nome)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(GD, "registry", SimpleNamespace(list=lambda: specs)), \
                mock.patch.object(gateway_admin, "registration", _fetch), \
                mock.patch.object(gateway_admin, "register_agent",
                                  lambda *a, **k: registrati.append((a, k))):
            rep = GD.check_all(repair=repair)
        return rep, registrati

    def test_humans_are_not_compared_nor_registered(self):
        """Un umano non sta nella config del gateway, e non è una dimenticanza:
        la sua matrice vive nel seed. Senza questa regola ogni persona
        risulterebbe `unregistered` e la riparazione la registrerebbe come
        agente — inventando un principal di un tipo che il modello non ha."""
        rep, registrati = self._run([_seed(name="davide", type="human")], {})
        self.assertEqual(rep["checked"], 0)
        self.assertEqual(registrati, [])

    def test_an_unregistered_agent_is_repaired(self):
        """Non c'è nessuna scelta a runtime da proteggere: l'entry manca per una
        registrazione fallita, e senza entry l'agente ha ZERO verbi."""
        rep, registrati = self._run(
            [_seed(denied_tools=["email.send"])],
            {"avvocato": {"agent": "avvocato", "registered": False}})
        self.assertEqual(rep["agents"][0]["status"], "unregistered")
        self.assertTrue(rep["agents"][0]["repaired"])
        self.assertEqual(len(registrati), 1)
        # La riparazione usa la stessa chiamata dell'install di un pack: una
        # policy sola, non due che divergono.
        self.assertEqual(registrati[0][0][0], "avvocato")
        self.assertEqual(registrati[0][1]["denied_tools"], ["email.send"])

    def test_a_divergence_is_never_repaired(self):
        """Il cuore della decisione: `missing`/`changed`/`extra` si SEGNALANO.
        La config viva può contenere una modifica intenzionale, e sovrascriverla
        al riavvio è l'evento muto che questa issue esiste per rendere
        visibile."""
        rep, registrati = self._run(
            [_seed(tool_permissions=["topic.open", "email.send"])],
            {"avvocato": _reg(allowed_tools=["*"])})
        self.assertEqual(rep["agents"][0]["status"], "diverged")
        self.assertEqual(registrati, [], "una divergenza è stata sovrascritta")

    def test_a_failed_repair_is_recorded_not_swallowed(self):
        def _boom(*_a, **_k):
            raise RuntimeError("500 dal gateway")

        from ..api import gateway_admin
        with mock.patch.object(GD, "registry", SimpleNamespace(list=lambda: [_seed()])), \
                mock.patch.object(gateway_admin, "registration",
                                  lambda n: {"agent": n, "registered": False}), \
                mock.patch.object(gateway_admin, "register_agent", _boom):
            rep = GD.check_all()
        self.assertFalse(rep["agents"][0]["repaired"])
        self.assertIn("500", rep["agents"][0]["error"])

    def test_an_unreachable_gateway_does_not_stop_the_walk(self):
        rep, _ = self._run([_seed(name="a"), _seed(name="b")],
                           {"a": RuntimeError("timeout"), "b": _reg(agent="b")})
        stati = {r["agent"]: r["status"] for r in rep["agents"]}
        self.assertEqual(stati["a"], "unavailable")
        self.assertEqual(stati["b"], "clean")


class BootReportTests(unittest.TestCase):
    def test_a_repair_is_logged_as_loudly_as_a_fault(self):
        """La condizione posta sulla riparazione: riparato ≠ pulito. Se
        l'auto-repair diventa muto, il fallimento dell'import torna invisibile —
        ed è il guasto che ha lasciato un agente senza verbi per quattro
        giorni."""
        rep = {"checked": 1, "agents": [{"agent": "avvocato", "status": "unregistered",
                                         "fields": [], "repaired": True}]}
        with mock.patch.object(GD, "check_all", return_value=rep), \
                self.assertLogs(GD.LOG, level="WARNING") as log:
            GD.report_at_boot()
        testo = "\n".join(log.output)
        self.assertIn("avvocato", testo)
        self.assertIn("RIPARATO", testo)

    def test_a_clean_run_does_not_shout(self):
        rep = {"checked": 2, "agents": [{"agent": "a", "status": "clean", "fields": []}]}
        with mock.patch.object(GD, "check_all", return_value=rep), \
                self.assertLogs(GD.LOG, level="INFO") as log:
            GD.report_at_boot()
        self.assertFalse([r for r in log.records if r.levelname == "WARNING"])

    def test_a_divergence_names_the_field_and_both_directions(self):
        rep = {"checked": 1, "agents": [{
            "agent": "ophelia", "status": "diverged",
            "fields": [{"field": "tool_permissions", "status": "extra",
                        "declared_only": [], "registered_only": ["*"]}]}]}
        with mock.patch.object(GD, "check_all", return_value=rep), \
                self.assertLogs(GD.LOG, level="WARNING") as log:
            GD.report_at_boot()
        testo = "\n".join(log.output)
        self.assertIn("ophelia", testo)
        self.assertIn("tool_permissions", testo)
        self.assertIn("*", testo)

    def test_the_report_never_raises(self):
        """Una diagnosi che impedisce l'avvio è peggio della diagnosi che
        manca."""
        with mock.patch.object(GD, "check_all", side_effect=RuntimeError("boom")):
            self.assertEqual(GD.report_at_boot()["checked"], 0)


class BootWiringTests(unittest.TestCase):
    """Il confronto esiste solo se qualcuno lo chiama: senza il cablaggio è un
    modulo che nessuno esegue, cioè esattamente la gamba mancante della #203."""

    def test_the_boot_calls_it_off_the_event_loop(self):
        import inspect
        from .. import main
        src = inspect.getsource(main)
        # `assertTrue` e non `assertIn`: sul fallimento `assertIn` stampa
        # l'INTERO modulo, e un messaggio d'errore di quattrocento righe è un
        # messaggio che nessuno legge.
        self.assertTrue("report_at_boot" in src,
                        "il boot non chiama il confronto seed↔gateway")
        # In un thread: N GET sincroni dentro il lifespan async fermerebbero
        # l'event loop di tutto il processo (#106).
        self.assertTrue("asyncio.to_thread(report_at_boot)" in src,
                        "il confronto gira sull'event loop invece che in un thread")


if __name__ == "__main__":
    unittest.main()
