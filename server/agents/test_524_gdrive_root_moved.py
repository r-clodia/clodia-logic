"""Tests for the Drive subtree confinement (gdrive_root), on google-pack's
gateway component (moved from clodia-tools `server/test_gdrive_root.py`
when the gateway's native Google client was removed, clodia-platform#524).
The native suite ran against `server/tools/gdrive_root.py`; this one runs the
same cases against the component's copy, with the perimeter given through the
host (`google_moved_524`).

Perché questi test sono la consegna quanto il codice. Il confinamento sostituisce
un confine che di solito applica Google (l'ACL di un account dedicato) con un
confine applicato dal NOSTRO codice. Un difetto qui non è un bug: è un accesso
concesso, e silenzioso. Due controlli scritti in questa sessione hanno già
fallito nella stessa direzione — il filtro di `topic.search` (che perdeva 27
righe SEAL-2) e i `deny_read` relativi di sysadmin (che non risolvevano nulla) —
entrambi perché nessun test camminava il ramo che concede.

Quindi qui si verifica soprattutto ciò che deve essere NEGATO, e in particolare i
tre modi non ovvi di uscire dal perimetro: la scorciatoia, la query arbitraria e
lo spostamento.
"""
from __future__ import annotations

import contextlib
import unittest
from unittest.mock import patch

from .google_moved_524 import cfg as _host_cfg
from .google_moved_524 import gr, rebind

ROOT = "ROOTFOLDER"
# Albero finto: figlio → genitori. `SHORTCUT` è una scorciatoia DENTRO la radice
# che punta a un file fuori: è il caso che un controllo ingenuo lascia passare.
TREE = {
    "dentro":        ["ROOTFOLDER"],
    "sub":           ["ROOTFOLDER"],
    "profondo":      ["sub"],
    "piu_profondo":  ["profondo"],
    "fuori":         ["ALTRA"],
    "ALTRA":         [],
    "ROOTFOLDER":    ["MYDRIVE"],
    "SHORTCUT":      ["ROOTFOLDER"],       # + shortcutDetails
    "ciclo_a":       ["ciclo_b"],
    "ciclo_b":       ["ciclo_a"],
    "orfano":        [],
}
SHORTCUTS = {"SHORTCUT": "fuori"}


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        if isinstance(self._val, Exception):
            raise self._val
        return self._val


class FakeFiles:
    def __init__(self, owner):
        self.owner = owner

    def get(self, fileId=None, fields=None, **kw):
        self.owner.calls += 1
        if fileId in self.owner.broken:
            return _Exec(RuntimeError("500 backendError"))
        if fileId not in TREE:
            return _Exec(RuntimeError("404 notFound"))
        out = {"id": fileId, "parents": list(TREE[fileId])}
        if fileId in SHORTCUTS:
            out["shortcutDetails"] = {"targetId": SHORTCUTS[fileId]}
        return _Exec(out)

    def list(self, **kw):
        return _Exec({"files": self.owner.rows})

    def update(self, **kw):
        self.owner.updates.append(kw)
        return _Exec({"id": kw.get("fileId")})

    def create(self, **kw):
        self.owner.creates.append(kw)
        return _Exec({"id": "nuovo", "name": (kw.get("body") or {}).get("name")})


class FakeDrive:
    def __init__(self, rows=None, broken=()):
        self.calls = 0
        self.rows = rows or []
        self.broken = set(broken)
        self.updates = []
        self.creates = []

    def files(self):
        return FakeFiles(self)


@contextlib.contextmanager
def _with(d):
    """Answer the Google calls of the gateway from the fake `d` (#524: the
    seam is `google_io`, the fake keeps the discovery-client shape)."""
    from .google_moved_524 import gdrive, google_io
    from .google_fake_524 import ServiceTransport
    rebind()
    with patch.object(google_io, "_transport", ServiceTransport(d)), \
            patch.object(gdrive, "gworkspace_accounts", lambda: ["conto"]):
        yield d


def _inside(d, *a):
    with _with(d):
        return gr.inside(*a)


def _keep_inside(d, *a):
    with _with(d):
        return gr.keep_inside(*a)


def _writable(d, *a):
    with _with(d):
        return gr.assert_writable_parent(*a)


def _assert_inside(d, *a):
    with _with(d):
        return gr.assert_inside(*a)


def _cfg(roots, egress=()):
    return _host_cfg(roots, egress)


class Base(unittest.TestCase):
    def setUp(self):
        rebind()
        gr.reset_cache()
        gr.reset_topic_cache()


class AncestryTests(Base):
    def test_an_account_without_an_entry_is_not_confined(self):
        """La compatibilità storica è un requisito: marte ha un account collegato
        e nessuna radice, e non deve cambiare comportamento."""
        with _cfg({"altro": [ROOT]}):
            self.assertTrue(_inside(FakeDrive(), "conto", "fuori"))
            self.assertFalse(gr.confined("conto"))

    def test_a_direct_child_is_inside(self):
        with _cfg({"conto": [ROOT]}):
            self.assertTrue(_inside(FakeDrive(), "conto", "dentro"))

    def test_the_root_itself_is_inside(self):
        with _cfg({"conto": [ROOT]}):
            self.assertTrue(_inside(FakeDrive(), "conto", ROOT))

    def test_a_grandchild_is_inside(self):
        with _cfg({"conto": [ROOT]}):
            self.assertTrue(_inside(FakeDrive(), "conto", "piu_profondo"))

    def test_a_file_in_another_folder_is_outside(self):
        with _cfg({"conto": [ROOT]}):
            self.assertFalse(_inside(FakeDrive(), "conto", "fuori"))

    def test_a_file_with_no_parents_is_outside(self):
        """La radice di «Il mio Drive» non è la cartella consentita."""
        with _cfg({"conto": [ROOT]}):
            self.assertFalse(_inside(FakeDrive(), "conto", "orfano"))


class TheThreeTraps(Base):
    def test_a_shortcut_inside_the_folder_does_not_grant_its_target(self):
        """La trappola numero uno.

        Una scorciatoia è un reindirizzamento: sta dentro la cartella, quindi un
        controllo che guarda solo i genitori la dichiara «dentro» e poi scarica
        ciò che sta FUORI. Chi può scrivere nella cartella condivisa potrebbe
        crearla, e non serve nessun altro permesso.
        """
        with _cfg({"conto": [ROOT]}):
            self.assertFalse(_inside(FakeDrive(), "conto", "SHORTCUT"))

    def test_passing_the_target_id_directly_is_also_refused(self):
        """L'altra metà della stessa trappola: aggirare la scorciatoia e chiedere
        direttamente il bersaglio non funziona, perché la risalita del bersaglio
        non raggiunge la radice."""
        with _cfg({"conto": [ROOT]}):
            self.assertFalse(_inside(FakeDrive(), "conto", "fuori"))

    def test_an_arbitrary_query_cannot_carry_anything_out(self):
        """La trappola numero due: `gdrive.list` accetta una query Drive libera.

        Il confinamento non può stare in una clausola aggiunta a quella stringa —
        la controlla il chiamante. Sta nel filtro degli id restituiti.
        """
        rows = [{"id": "dentro", "parents": [ROOT], "mimeType": "text/plain"},
                {"id": "fuori", "parents": ["ALTRA"], "mimeType": "text/plain"},
                {"id": "SHORTCUT", "parents": [ROOT],
                 "mimeType": gr.SHORTCUT_MIME}]
        with _cfg({"conto": [ROOT]}):
            kept = _keep_inside(FakeDrive(), "conto", rows)
        self.assertEqual([r["id"] for r in kept], ["dentro"])

    def test_a_row_that_cannot_prove_it_is_not_a_shortcut_is_verified(self):
        """Il difetto che il test sopra ha scoperto, fissato perché non torni.

        Una riga senza `shortcutDetails` è indistinguibile da una riga in cui quel
        campo non è stato CHIESTO. Fidarsi dell'assenza faceva passare la
        scorciatoia dalla porta dell'ottimizzazione: il percorso rapido annullava
        il controllo che l'ottimizzazione non doveva toccare.
        """
        inconcludente = [{"id": "SHORTCUT", "parents": [ROOT]}]   # nessun campo utile
        d = FakeDrive()
        with _cfg({"conto": [ROOT]}):
            kept = _keep_inside(d, "conto", inconcludente)
        self.assertEqual(kept, [], "una riga inconcludente non va creduta")
        self.assertGreater(d.calls, 0, "deve essere stata verificata all'API")

    def test_moving_a_file_out_is_refused_at_the_destination(self):
        """La trappola numero tre: per esfiltrare non serve leggere.

        Spostare un file dalla cartella condivisa a una fuori lo porta là dove il
        controllo non arriva più. Servono ENTRAMBI gli estremi.
        """
        from .google_moved_524 import gdrive
        d = FakeDrive()
        # (The whitelist check is the gateway's, before the component runs.)
        with _cfg({"conto": [ROOT]}), _with(d):
            with self.assertRaises(gr.OutsideRoot) as ctx:
                gdrive.move("dentro", "ALTRA")
        self.assertIn("destinazione", str(ctx.exception))
        self.assertEqual(d.updates, [], "nessuno spostamento deve essere partito")


class FailClosedTests(Base):
    def test_an_api_error_means_outside(self):
        """`errore == fuori`. È la sola direzione accettabile: un 500 di Google
        non deve aprire il Drive."""
        with _cfg({"conto": [ROOT]}):
            self.assertFalse(_inside(FakeDrive(broken=["profondo"]),
                                       "conto", "piu_profondo"))

    def test_a_cycle_terminates_and_denies(self):
        with _cfg({"conto": [ROOT]}):
            self.assertFalse(_inside(FakeDrive(), "conto", "ciclo_a"))

    def test_an_unknown_id_is_outside_not_an_open_door(self):
        with _cfg({"conto": [ROOT]}):
            self.assertFalse(_inside(FakeDrive(), "conto", "inesistente"))

    def test_a_malformed_config_does_not_disable_the_check(self):
        """Una configurazione scritta male non deve significare «nessun limite»
        per un account che ne ha uno valido, né esplodere."""
        with _cfg({"conto": "ROOTFOLDER"}):          # stringa invece di lista
            self.assertEqual(gr.roots_for("conto"), [ROOT])
        from .google_moved_524 import HOST
        with _cfg({}):
            HOST.perimeter.roots = ["non-un-dizionario"]
            self.assertEqual(gr.roots_for("conto"), [])


class ConfigTests(Base):
    def test_a_star_entry_adds_to_the_specific_one(self):
        """`*` è un minimo comune, non un default che una voce specifica
        sostituisce: chi scrive la radice di un account non si aspetta di perdere
        quella globale."""
        with _cfg({"*": ["GLOB"], "conto": [ROOT]}):
            self.assertEqual(gr.roots_for("conto"), ["GLOB", ROOT])

    def test_no_key_at_all_confines_nothing(self):
        with _host_cfg(None):
            self.assertEqual(gr.roots_for("conto"), [])


class CostTests(Base):
    def test_a_direct_child_costs_no_api_call(self):
        """I `parents` sono già nei campi degli elenchi Drive. Se il caso comune
        costasse una chiamata per riga, un elenco di 50 file ne costerebbe 50 e
        la misura si farebbe togliere per lentezza."""
        d = FakeDrive()
        with _cfg({"conto": [ROOT]}):
            self.assertTrue(_inside(d, "conto", "dentro",
                          {"id": "dentro", "parents": [ROOT],
                           "mimeType": "text/plain"}))
        self.assertEqual(d.calls, 0)

    def test_an_unconfined_account_pays_nothing(self):
        d = FakeDrive()
        with _cfg({}):
            _keep_inside(d, "conto", [{"id": "fuori", "parents": ["ALTRA"],
                                         "mimeType": "text/plain"}])
        self.assertEqual(d.calls, 0)


class FastPathTests(Base):
    """Il percorso rapido è un rischio al contrario: se salta troppo, non confina.

    `guard_id` esce subito quando nessuna radice è configurata — serve perché
    altrimenti risolverebbe l'account e toccherebbe il vault su ogni chiamata di
    gdocs/gsheets anche dove la funzione non ha niente da fare. Ma un'uscita
    anticipata sbagliata è un confinamento che non c'è.
    """

    def test_it_exits_early_only_when_nothing_is_configured(self):
        from .google_moved_524 import gdrive
        called = []
        with _cfg({}), patch.object(gdrive, "_resolve_account",
                                    lambda *_a: called.append(1) or "conto"):
            gr.guard_id(None, "fuori", "gdocs.read")
        self.assertEqual(called, [], "senza radici non deve toccare il vault")

    def test_with_a_root_for_another_account_it_still_resolves(self):
        """La voce è di un altro account, ma per SAPERLO bisogna risolvere il
        proprio: uscire prima significherebbe non confinare mai quando la
        configurazione nomina gli account uno per uno."""
        from .google_moved_524 import gdrive
        called = []
        with _cfg({"altro": [ROOT]}), \
                patch.object(gdrive, "_resolve_account",
                             lambda *_a: (called.append(1), "conto")[1]):
            gr.guard_id(None, "fuori", "gdocs.read")
        self.assertEqual(called, [1])


class WriteDestinationTests(Base):
    def test_a_write_with_no_folder_lands_in_the_root(self):
        """Rifiutare sarebbe una regressione gratuita: con una sola radice la
        destinazione ovvia esiste. Senza questo, `upload` senza cartella
        scriverebbe nella radice di «Il mio Drive» — fuori dal perimetro."""
        with _cfg({"conto": [ROOT]}):
            self.assertEqual(
                _writable(FakeDrive(), "conto", None, "gdrive.upload"),
                ROOT)

    def test_with_two_roots_the_destination_must_be_named(self):
        with _cfg({"conto": [ROOT, "SECONDA"]}):
            with self.assertRaises(gr.OutsideRoot):
                _writable(FakeDrive(), "conto", None, "gdrive.upload")

    def test_a_destination_outside_is_refused(self):
        with _cfg({"conto": [ROOT]}):
            with self.assertRaises(gr.OutsideRoot):
                _writable(FakeDrive(), "conto", "ALTRA", "gdrive.upload")

    def test_an_unconfined_account_keeps_the_caller_choice(self):
        with _cfg({}):
            self.assertIsNone(
                _writable(FakeDrive(), "conto", None, "gdrive.upload"))


class RefusalMessageTests(Base):
    def test_the_refusal_says_what_to_do_instead(self):
        """Un rifiuto che non porta l'alternativa produce il guasto di oggi: un
        agente che riprova la stessa cosa, o che riferisce «permessi» a un umano
        che non ha modo di sapere cosa gli manca."""
        with _cfg({"conto": [ROOT]}):
            try:
                _assert_inside(FakeDrive(), "conto", "fuori", "gdrive.download")
            except gr.OutsideRoot as e:
                msg = str(e)
        self.assertIn("gdrive.download", msg)
        self.assertIn(ROOT, msg)
        self.assertIn("spostato", msg)     # dice cosa fare
        self.assertIn("aggirabile", msg)   # e che riprovare non serve


CAL = "agenda@tomato.blue"


class CalendarTests(Base):
    def test_the_calendar_is_closed_when_a_folder_root_is_set(self):
        """Il calendario non sta in una cartella: nessuna radice di Drive dice
        qualcosa su di esso. Lasciarlo passare renderebbe FALSA l'affermazione
        «l'agente vede solo quella cartella» — l'agenda sarebbe leggibile."""
        with _cfg({"conto": [ROOT]}):
            with self.assertRaises(gr.OutsideRoot) as ctx:
                gr.assert_calendar_allowed("conto", "gcalendar.list_events", CAL)
        self.assertIn("calendario", str(ctx.exception))

    def test_an_unconfined_account_keeps_the_calendar(self):
        with _cfg({}):
            gr.assert_calendar_allowed("conto", "gcalendar.list_events", CAL)

    def test_a_declared_calendar_passes_on_a_confined_account(self):
        """clodia-platform#429. Il confinamento di Drive non è un argomento per
        chiudere il calendario per sempre: è un argomento per pretendere che il
        calendario sia dichiarato, come lo sono la cartella e la casella."""
        with _cfg({"conto": [ROOT]}, egress=[f"gcal:{CAL}"]):
            gr.assert_calendar_allowed("conto", "gcalendar.list_events", CAL)

    def test_another_calendar_is_still_refused(self):
        """La voce approva UN calendario, non il servizio."""
        with _cfg({"conto": [ROOT]}, egress=[f"gcal:{CAL}"]):
            with self.assertRaises(gr.OutsideRoot):
                gr.assert_calendar_allowed("conto", "gcalendar.list_events",
                                           "altro@tomato.blue")

    def test_listing_the_calendars_needs_at_least_one_declared(self):
        """`list_calendars` non nomina un calendario: senza nessuna voce
        elencherebbe l'agenda intera, che è ciò che il confinamento promette di
        non mostrare."""
        with _cfg({"conto": [ROOT]}):
            with self.assertRaises(gr.OutsideRoot):
                gr.assert_calendar_allowed("conto", "gcalendar.list_calendars")
        with _cfg({"conto": [ROOT]}, egress=[f"gcal:{CAL}"]):
            gr.assert_calendar_allowed("conto", "gcalendar.list_calendars")

    def test_the_listing_is_reduced_to_the_declared_calendars(self):
        righe = [{"id": CAL}, {"id": "altro@tomato.blue"},
                 {"id": "personale@gmail.com", "primary": True}]
        with _cfg({"conto": [ROOT]}, egress=[f"gcal:{CAL}"]):
            tenuti = [r["id"] for r in gr.keep_calendars("conto", righe)]
        self.assertEqual(tenuti, [CAL])

    def test_the_listing_is_untouched_when_unconfined(self):
        righe = [{"id": CAL}, {"id": "altro@tomato.blue"}]
        with _cfg({}):
            self.assertEqual(gr.keep_calendars("conto", righe), righe)

    def test_the_refusal_names_the_real_remedy_and_not_oauth(self):
        """Il messaggio è metà del fix. Quello vecchio diceva «serve un account
        non confinato» e ha prodotto quattro riconnessioni OAuth inutili: lo
        scope Calendar era già nel consenso, e questo controllo non lo guarda.
        Il rifiuto deve nominare la voce di whitelist, l'id chiesto, e dire
        esplicitamente che riconnettere non serve."""
        with _cfg({"conto": [ROOT]}, egress=[f"gcal:{CAL}"]):
            try:
                gr.assert_calendar_allowed("conto", "gcalendar.list_events",
                                           "altro@tomato.blue")
            except gr.OutsideRoot as e:
                msg = str(e)
        self.assertIn("gcal:", msg)                  # la voce da aggiungere
        self.assertIn("altro@tomato.blue", msg)      # cosa è stato chiesto
        self.assertIn(CAL, msg)                      # cosa è approvato qui
        self.assertIn("OAuth", msg)                  # e che NON è quello


class NativeDocTests(Base):
    def test_a_fresh_doc_is_adopted_into_the_root(self):
        """Le API Docs/Sheets creano SEMPRE nella radice di «Il mio Drive» e non
        accettano un genitore. Rifiutare toglierebbe un verbo utile; lasciar
        stare romperebbe il perimetro. Quindi si crea e si adotta."""
        from .google_moved_524 import gdrive
        d = FakeDrive()
        with _cfg({"conto": [ROOT]}), \
                _with(d), \
                patch.object(gdrive, "_resolve_account", return_value="conto"):
            dest = gr.adopt("conto", "orfano", "gdocs.create")
        self.assertEqual(dest, ROOT)
        self.assertEqual(len(d.updates), 1)
        self.assertEqual(d.updates[0]["addParents"], ROOT)

    def test_adoption_is_a_no_op_when_unconfined(self):
        from .google_moved_524 import gdrive
        d = FakeDrive()
        with _cfg({}), patch.object(gdrive, "_resolve_account", return_value="conto"):
            self.assertIsNone(gr.adopt("conto", "orfano", "gdocs.create"))
        self.assertEqual(d.updates, [])

    def test_reading_a_doc_outside_the_folder_is_refused(self):
        """Il buco che rende inutile confinare solo gdrive: `gdocs.read` gira
        sullo STESSO token, e un id di Doc è un id di Drive."""
        from .google_moved_524 import gdrive
        d = FakeDrive()
        with _cfg({"conto": [ROOT]}), \
                _with(d), \
                patch.object(gdrive, "_resolve_account", return_value="conto"):
            with self.assertRaises(gr.OutsideRoot):
                gr.guard_id("conto", "fuori", "gdocs.read")

    def test_reading_a_doc_inside_the_folder_is_allowed(self):
        from .google_moved_524 import gdrive
        d = FakeDrive()
        with _cfg({"conto": [ROOT]}), \
                _with(d), \
                patch.object(gdrive, "_resolve_account", return_value="conto"):
            gr.guard_id("conto", "dentro", "gdocs.read")      # non solleva


class VerbCoverageTests(Base):
    """Ogni verbo che accetta un id deve passare dal controllo.

    Non è pedanteria: il modo in cui questo confinamento fallisce non è un
    controllo sbagliato, è un verbo dimenticato. Questo test si rompe quando
    qualcuno ne aggiunge uno nuovo senza guardia.
    """

    def test_every_gdrive_verb_taking_an_id_is_guarded(self):
        import inspect
        from .google_moved_524 import gdrive
        for name in ("download", "share", "rename", "move", "upload", "mkdir",
                     "list_files", "search"):
            src = inspect.getsource(getattr(gdrive, name))
            self.assertIn("gdrive_root.", src, f"{name} non passa dal confinamento")

    def test_every_gdocs_and_gsheets_verb_is_guarded(self):
        import inspect
        from .google_moved_524 import gdocs, gsheets
        for mod, verbs in ((gdocs, ("read", "append_text", "replace_text", "create")),
                           (gsheets, ("read", "list_tabs", "add_tab", "append_rows",
                                      "write_range"))):
            for name in verbs:
                src = inspect.getsource(getattr(mod, name))
                self.assertIn("gdrive_root.", src,
                              f"{mod.__name__}.{name} non passa dal confinamento")

    def test_every_gcalendar_verb_is_guarded(self):
        import inspect
        from .google_moved_524 import gcalendar
        for name in ("list_calendars", "list_events", "create_event",
                     "update_event", "delete_event", "freebusy"):
            src = inspect.getsource(getattr(gcalendar, name))
            self.assertIn("guard_calendar", src, f"gcalendar.{name} non è guardato")

    def test_every_gcalendar_verb_declares_which_calendar_it_touches(self):
        """Guardarlo non basta: il guard deve ricevere il `calendar_id`.

        Chiamarlo senza è la regressione silenziosa di questa feature — il guard
        interpreta l'assenza come «la chiamata non nomina un calendario» (il
        caso di `list_calendars`) e lascia passare QUALUNQUE calendario non
        appena uno solo è approvato. Il controllo sembrerebbe ancora in piedi.
        """
        import inspect
        from .google_moved_524 import gcalendar
        for name in ("list_events", "create_event", "update_event",
                     "delete_event", "freebusy"):
            src = inspect.getsource(getattr(gcalendar, name))
            self.assertIn("guard_calendar(account,", src)
            riga = [r for r in src.splitlines() if "guard_calendar(" in r][0]
            self.assertIn("calendar_id", riga,
                          f"gcalendar.{name} non dice QUALE calendario tocca")


if __name__ == "__main__":
    unittest.main()
