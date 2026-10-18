"""Il perimetro Drive è per TOPIC, non per account — on google-pack's gateway
component (moved from clodia-tools `server/test_gdrive_root_per_topic.py`
when the gateway's native Google client was removed, clodia-platform#524). The
signed channel claim reaches the component as `host.channel()`
(`<tier>/<topic>`), which `_Chat` sets on the fake host.

La regola, chiesta da Davide: *«il path che metto come remote diventa la root di
confine per gli accessi dal topic»*. La cartella del remote di un topic è il
perimetro delle chiamate che avvengono dentro quel canale.

Perché stamattina avevo argomentato il contrario, e cosa è cambiato. L'obiezione
era che chi può creare un remote si allarga il perimetro da sé, e l'ho verificata
invece di ripeterla: l'endpoint della webui chiedeva `_require_member`, quindi
**qualunque partecipante** — Matteo — poteva puntare il remote a `30-legale`. Il
disegno regge solo con la sua conseguenza: impostare, cambiare o togliere un
remote Drive diventa un'azione da **admin**, perché non è più una preferenza ma
una dichiarazione di autorità.

Ciò che lo rende un controllo e non una convenzione: il topic della chiamata
arriva nel claim `chat` FIRMATO dall'agent-server. Un agente non può dichiarare
il topic di un altro per prenderne il perimetro.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from .google_moved_524 import HOST, gr, rebind

TOPIC_FOLDER = "50EXEC"        # la cartella del remote del topic
ALTRA = "30LEGALE"             # una sorella, fuori dal perimetro
ACCOUNT_ROOT = "PROGETTO"      # radice d'account (tetto), se impostata

TREE = {
    "dentro-topic": ["50EXEC"],
    "50EXEC": ["PROGETTO"],
    "in-legale": ["30LEGALE"],
    "30LEGALE": ["PROGETTO"],
    "PROGETTO": ["MYDRIVE"],
    # La radice di «Il mio Drive» e le altre cime sono nell'albero con genitori
    # VUOTI, non assenti. Senza, la risalita finiva in un 404 e il verdetto
    # «fuori» arrivava dal fail-closed invece che dall'aver esaurito gli
    # antenati: il test passava per la ragione sbagliata e non avrebbe visto una
    # regressione nella logica di discendenza. Stesso difetto trovato oggi in un
    # test costruito su un verbo inesistente.
    "MYDRIVE": [],
    "altrove": ["ALTRO"],
    "ALTRO": [],
}


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        if isinstance(self.v, Exception):
            raise self.v
        return self.v


class FakeDrive:
    def __init__(self):
        self.calls = 0

    def files(self):
        return self

    def get(self, fileId=None, **kw):
        self.calls += 1
        if fileId not in TREE:
            return _Exec(RuntimeError("404"))
        return _Exec({"id": fileId, "parents": list(TREE[fileId])})

    def list(self, **kw):
        return _Exec({"files": []})


def _inside(d, *a):
    """`gr.inside` with the Google calls answered by the fake `d` (#524)."""
    from .google_moved_524 import google_io
    from .google_fake_524 import ServiceTransport
    with patch.object(google_io, "_transport", ServiceTransport(d)):
        return gr.inside(*a)


def _channel_of(chat):
    """`chan:<tier>:<topic>:<agent>` → `<tier>/<topic>`, as the gateway's
    `whitelist.current_channel` reads the signed claim; anything else → None."""
    parts = str(chat or "").split(":")
    if len(parts) >= 4 and parts[0] == "chan" and parts[1] and parts[2]:
        return f"{parts[1]}/{parts[2]}"
    return None


class _Chat:
    def __init__(self, v):
        self.v = v

    def __enter__(self):
        rebind()
        self.old = HOST.chan
        HOST.chan = _channel_of(self.v)
        return self

    def __exit__(self, *a):
        HOST.chan = self.old
        return False


def _env(account_roots=None, topic_folder=TOPIC_FOLDER):
    roots = {"conto": list(account_roots)} if account_roots else {}
    return (patch.object(HOST.perimeter, "roots", roots),
            patch.object(HOST.perimeter, "egress", []),
            patch.object(gr, "_topic_drive_folder",
                         lambda tier, name: topic_folder))


class Base(unittest.TestCase):
    def setUp(self):
        rebind()
        gr.reset_cache()
        gr.reset_topic_cache()

    def run_with(self, ctx, fn):
        for c in ctx:
            c.start()
        try:
            return fn()
        finally:
            [c.stop() for c in ctx]


class PerTopicTests(Base):
    CHAN = "chan:SEAL-1:proof-of-flex-2:messaggero"

    def test_the_fixture_walks_to_the_top_without_errors(self):
        """Guardia sul test stesso: se la risalita fallisce, ogni verdetto
        «fuori» sotto è indistinguibile da un fail-closed e non prova niente."""
        d = FakeDrive()
        cur, visti = "in-legale", []
        for _ in range(6):
            r = d.get(fileId=cur).execute()
            visti.append(cur)
            ps = r.get("parents") or []
            if not ps:
                break
            cur = ps[0]
        self.assertEqual(visti, ["in-legale", "30LEGALE", "PROGETTO", "MYDRIVE"])

    def test_inside_the_channel_the_perimeter_is_the_topics_remote(self):
        def go():
            with _Chat(self.CHAN):
                roots, fonte = gr.roots_for_call("conto")
                self.assertEqual(roots, [TOPIC_FOLDER])
                self.assertEqual(fonte, "topic")
                d = FakeDrive()
                self.assertTrue(_inside(d, "conto", "dentro-topic"))
                self.assertFalse(_inside(d, "conto", "in-legale"))
        self.run_with(_env(), go)

    def test_a_sibling_folder_of_the_remote_is_outside(self):
        """Il caso concreto: `30-legale` e `40-budget` sono sorelle di
        `50-execution`. Il topic espone la seconda, e sono precisamente le prime
        due che non devono finire in un canale con dei collaboratori."""
        def go():
            with _Chat(self.CHAN):
                self.assertFalse(_inside(FakeDrive(), "conto", "in-legale"))
                self.assertFalse(_inside(FakeDrive(), "conto", "30LEGALE"))
        self.run_with(_env(), go)

    def test_the_parent_of_the_remote_is_outside_too(self):
        """Confinare a una cartella non concede il suo genitore: altrimenti
        confinare a una sottocartella non confinerebbe niente."""
        def go():
            with _Chat(self.CHAN):
                self.assertFalse(_inside(FakeDrive(), "conto", "PROGETTO"))
        self.run_with(_env(), go)

    def test_outside_any_channel_the_account_roots_apply(self):
        """Un job o una DM non hanno un topic. Il fallback è l'account, non
        «tutto»: se il fallback fosse permissivo, basterebbe uscire dal canale
        per uscire dal perimetro."""
        def go():
            with _Chat(None):
                roots, fonte = gr.roots_for_call("conto")
                self.assertEqual(roots, [ACCOUNT_ROOT])
                self.assertEqual(fonte, "account")
        self.run_with(_env(account_roots=[ACCOUNT_ROOT]), go)

    def test_a_channel_without_a_drive_remote_falls_back_to_the_account(self):
        """Negare qui romperebbe un uso legittimo: allegare a una mail un file
        preso da Drive, in un topic che non ha un remote."""
        def go():
            with _Chat(self.CHAN):
                roots, fonte = gr.roots_for_call("conto")
                self.assertEqual(fonte, "account")
                self.assertEqual(roots, [])
        self.run_with(_env(topic_folder=None), go)

    def test_an_unreadable_topic_meta_does_not_invent_a_perimeter(self):
        """Se il meta non si legge non si finge di avere una radice dal topic: si
        ricade sull'account. Inventare un perimetro da un dato non letto è la
        direzione d'errore sbagliata."""
        def go():
            with _Chat(self.CHAN):
                _, fonte = gr.roots_for_call("conto")
                self.assertEqual(fonte, "account")
        self.run_with(_env(topic_folder=None), go)

    def test_a_malformed_channel_claim_falls_back(self):
        def go():
            with _Chat("chan:soloUnPezzo"):
                _, fonte = gr.roots_for_call("conto")
                self.assertEqual(fonte, "account")
        self.run_with(_env(), go)


class HostPerimeterTests(Base):
    """The topic's folder really comes from the host (`perimeter.
    topic_drive_folders`), and a failing read there invents nothing."""

    def test_the_declared_folder_is_the_perimeter(self):
        with patch.object(HOST.perimeter, "topics", {("SEAL-1", "proof-of-flex-2"):
                                                     [TOPIC_FOLDER]}), \
                patch.object(HOST.perimeter, "roots", {}), \
                patch.object(HOST.perimeter, "egress", []), \
                _Chat(PerTopicTests.CHAN):
            self.assertEqual(gr.roots_for_call("conto"), ([TOPIC_FOLDER], "topic"))

    def test_an_unreadable_meta_falls_back_to_the_account(self):
        def boom(tier, name):
            raise OSError("meta unreadable")
        with patch.object(HOST.perimeter, "topic_drive_folders", boom), \
                patch.object(HOST.perimeter, "roots", {}), \
                patch.object(HOST.perimeter, "egress", []), \
                _Chat(PerTopicTests.CHAN):
            self.assertEqual(gr.roots_for_call("conto"), ([], "account"))


class CeilingTests(Base):
    CHAN = "chan:SEAL-1:proof-of-flex-2:messaggero"

    def test_account_roots_act_as_a_ceiling_not_an_alternative(self):
        """Un topic non deve poter sfondare il pavimento posato dall'owner.

        Con radici d'account impostate, la cartella del topic non le sostituisce:
        la verifica interseca, quindi il perimetro effettivo non è più largo del
        minore dei due.
        """
        def go():
            with _Chat(self.CHAN):
                roots, fonte = gr.roots_for_call("conto")
                self.assertIn("tetto", fonte)
                self.assertIn(ACCOUNT_ROOT, roots)
                # una cartella fuori da ENTRAMBI resta fuori
                self.assertFalse(_inside(FakeDrive(), "conto", "altrove"))
        self.run_with(_env(account_roots=[ACCOUNT_ROOT]), go)


class SignedClaimTests(Base):
    def test_the_topic_comes_from_the_signed_claim_only(self):
        """Il perimetro non può dipendere da un argomento della chiamata: sarebbe
        la parola dell'agente su sé stesso. Deriva da `current_channel()`, che
        legge il claim `chat` verificato."""
        import inspect
        src = inspect.getsource(gr.roots_for_call)
        self.assertIn("current_channel()", src)
        self.assertNotIn("arguments", src)


if __name__ == "__main__":
    unittest.main()
