"""Una tripla `agent|instance|verb` può coprire PIÙ richieste pendenti.

L'identità di un gate non contiene l'argomento: sette `egress.allow` verso sette
destinazioni diverse sono sette richieste e una sola tripla
(clodia-platform#232). Chi decide vede una card per richiesta, ma il backend
riceve solo la tripla — e la capability che conia sblocca **tutte** le chiamate
in attesa con quella tripla, non quella che si stava guardando.

Da qui i due invarianti misurati sotto:

  1. **il titolo si verifica su ognuna.** Se fra le pendenti ce n'è una nata in
     un'altra stanza, l'owner della prima non ha titolo a sbloccarla: approvando
     il verbo la sbloccherebbe comunque. Guardare solo la prima della lista
     rende decorativa l'autorità dell'owner dell'altra stanza (voce 24), e la
     lista non ha nemmeno un ordine garantito — «la prima» non vuol dire «la
     più vecchia».

  2. **non si ricorda una destinazione nella stanza sbagliata.** «Approva sempre
     qui» scrive una voce permanente: sceglierla dalla prima pendente la
     scriverebbe in una stanza che non ha mai chiesto niente. Nel dubbio non si
     ricorda, e si dice perché — l'approvazione di stavolta resta valida.

Con UNA sola pendente il comportamento resta identico a prima: è il caso
normale, e questi test lo fissano insieme agli altri due.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from . import gate as G


def _req(chat: str, verb: str = "egress.allow", klass: str = "walls") -> dict:
    return {"agent": "sysadmin", "instance": "-", "verb": verb,
            "class": klass, "chat": chat}


class _Risposta:
    def __init__(self, code=200, corpo=None):
        self.status_code = code
        self._c = corpo if corpo is not None else {"ok": True}
        self.text = json.dumps(self._c)

    def json(self):
        return self._c


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def _chiama(chi, body, pendenti, *, admin_ok=False,
            owner_di=(("SEAL-1", "acme"),)):
    """Chiama `/api/gate/approve` con una coda di pendenti data."""
    chiamate = []

    def _gw(metodo, path, principal, corpo=None):
        chiamate.append((metodo, path, corpo))
        if path == "/pending":
            return _Risposta(200, {"requests": pendenti})
        if path == "/allow":
            return _Risposta(200, {"remembered": True, "uri": "…"})
        return _Risposta(200, {"ok": True})

    def _owner(principal, scope):
        return tuple(scope) in {tuple(s) for s in owner_di}

    with patch.object(G, "_gw", _gw), \
            patch.object(G, "_is_scope_owner", _owner), \
            patch.object(G.admin, "is_admin", lambda p: admin_ok), \
            patch.object(G, "_principal_from_request", lambda r: chi), \
            patch.object(G.pki, "mint_capability",
                         lambda *a, **k: {"token": "ccap1…", "jti": "j"}), \
            patch.object(G, "_post_outcome", lambda *a, **k: None):
        risposta = asyncio.run(G.approve(_Req(body)))
    corpo = json.loads(bytes(risposta.body).decode())
    return risposta.status_code, corpo, chiamate


BASE = {"agent": "sysadmin", "instance": "-", "verb": "egress.allow"}


class DuplicatePending(unittest.TestCase):
    def test_una_sola_pendente_si_approva_come_prima(self):
        """Il caso normale non cambia: una richiesta, l'owner della sua stanza."""
        code, corpo, chiamate = _chiama(
            "davide", {**BASE}, [_req("chan:SEAL-1:acme:sysadmin")])
        self.assertEqual(code, 200, corpo)
        self.assertIn(("POST", "/grant", {"agent": "sysadmin", "instance": "-",
                                          "verb": "egress.allow",
                                          "token": "ccap1…"}), chiamate)

    def test_una_pendente_di_un_altra_stanza_blocca_l_approvazione(self):
        """Approvare il verbo sbloccherebbe anche la richiesta della stanza B:
        l'owner di A non ha titolo su quella, quindi il sì non passa."""
        code, corpo, chiamate = _chiama(
            "davide", {**BASE},
            [_req("chan:SEAL-1:acme:sysadmin"),
             _req("chan:SEAL-1:altra-stanza:sysadmin")])
        self.assertEqual(code, 403, corpo)
        self.assertIn("altra-stanza", corpo.get("detail", ""))
        self.assertIn("2 richieste", corpo.get("detail", ""))
        self.assertNotIn("/grant", [c[1] for c in chiamate],
                         "nessuna capability va coniata su un titolo mancante")

    def test_stessa_stanza_due_volte_resta_approvabile(self):
        """Due tentativi sulla stessa destinazione, nella stessa stanza, non
        sono un'ambiguità di titolo: bloccarli ricreerebbe lo stallo del 17 ago
        (sette round di gate a vuoto), che è il difetto da cui veniamo."""
        code, corpo, _ = _chiama(
            "davide", {**BASE},
            [_req("chan:SEAL-1:acme:sysadmin"), _req("chan:SEAL-1:acme:sysadmin")])
        self.assertEqual(code, 200, corpo)

    def test_ricordare_qui_non_scrive_nella_stanza_sbagliata(self):
        """`remember=topic` con pendenti da stanze diverse: non si indovina.

        L'approvazione resta valida per stavolta, e la risposta dice perché non
        è stata ricordata — un sì che si crede permanente e non lo è ritorna
        domani e sembra un gate rotto.
        """
        code, corpo, chiamate = _chiama(
            "davide", {**BASE, "remember": "topic"},
            [_req("chan:SEAL-1:acme:sysadmin"),
             _req("chan:SEAL-1:acme:sysadmin")],
        )
        self.assertEqual(code, 200, corpo)
        self.assertTrue(corpo["memory"]["remembered"])

        # …e ora la coda mescola due stanze di cui chi decide è owner di
        # ENTRAMBE: il titolo c'è, quindi il sì passa. La MEMORIA no: «sempre
        # qui» non sa quale delle due sia «qui».
        code, corpo, chiamate = _chiama(
            "davide", {**BASE, "remember": "topic"},
            [_req("chan:SEAL-1:acme:sysadmin"),
             _req("chan:SEAL-1:altra-stanza:sysadmin")],
            owner_di=(("SEAL-1", "acme"), ("SEAL-1", "altra-stanza")),
        )
        self.assertEqual(code, 200, corpo)
        self.assertFalse(corpo["memory"]["remembered"])
        self.assertIn("stanze diverse", corpo["memory"]["error"])
        self.assertNotIn("/allow", [c[1] for c in chiamate],
                         "nessuna voce permanente su una stanza indovinata")


if __name__ == "__main__":
    unittest.main()
