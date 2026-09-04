"""Un AGENTE che chiama un endpoint di piattaforma veniva autorizzato come umano.

`require_authz` è la guardia delle azioni che l'agent-server esegue localmente:
verifica il principal del Bearer e chiede al gateway se il verbo è consentito.
Chiedeva però **sempre** con un token coniato on-behalf sul nome del chiamante
(`_token`), e quel claim dice al gateway «autorizza sul RUOLO umano». Per un
agente il ruolo umano non esiste: `admin.is_admin("sysadmin")` è False → `user`
→ negato, qualunque grant abbia l'agente.

Il caso della #297: `sysadmin` ha `packs.*` con verbo `own`, chiama
`packs.setup_done`, il gateway inoltra a questo endpoint, e l'endpoint chiedeva
al gateway l'autorizzazione di un *umano* chiamato sysadmin. Il 403 che ne
risultava era indistinguibile da un grant mancante — la cosa che l'issue chiede
di rendere distinguibile.

Due difetti, due misure:
- l'identità dell'agente arriva al PDP (si INOLTRA il suo token, non se ne conia
  uno umano) e la sessione umana della webui continua a funzionare come prima;
- la rotta `setup-done` chiede il verbo che implementa (`packs.setup_done`) e non
  `packs.import_url` — quel prestito valeva come «admin-only» quando l'unico
  chiamante era un umano, e nega a chi ha il grant giusto.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from starlette.requests import Request

from . import gateway_pdp, packs

_TOKEN = "ckt1.token-di-sysadmin"


def _richiesta(token: str = _TOKEN) -> Request:
    return Request({"type": "http", "method": "POST",
                    "path": "/clodia/packs/studio-legale/setup-done",
                    "headers": [(b"authorization", f"Bearer {token}".encode())]})


class _Risposta:
    def __init__(self, allowed: bool = True, status: int = 200):
        self.status_code, self._allowed = status, allowed
        self.text = ""

    def json(self) -> dict:
        return {"allowed": self._allowed}


class _Gateway:
    """Il PDP, sostituito: ciò che si misura è COSA gli viene chiesto e con
    QUALE identità."""

    def __init__(self, allowed: bool = True):
        self.chiamate: list[dict] = []
        self._allowed = allowed

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.chiamate.append({"url": url, "headers": headers or {},
                              "json": json or {}})
        return _Risposta(self._allowed)

    @property
    def bearer(self) -> str:
        return (self.chiamate[-1]["headers"].get("Authorization") or "")[7:]


class _Chiamante:
    """`type` del principal nella registry: `ai` per un agente, `human` per una
    persona. È la stessa distinzione che usa `loader.get_by_telegram`."""

    def __init__(self, nome: str, tipo: str):
        self.name, self.type = nome, tipo


def _mondo(gw: _Gateway, chiamante: _Chiamante | None, monetazione=None):
    """Contesto comune: token verificato, registry, HTTP del gateway."""
    def _mint(*a, **k):
        if monetazione is not None:
            monetazione.append((a, k))
            return "ckt1.coniato-on-behalf"
        raise AssertionError("nessun token doveva essere coniato per un agente")

    return [
        patch.object(gateway_pdp.pki, "verify_session_token",
                     lambda _t: {"agent": chiamante.name} if chiamante else {}),
        patch.object(gateway_pdp.pki, "mint_session_token", _mint),
        patch.object(gateway_pdp.registry, "get_by_name", lambda _n: chiamante),
        patch.object(gateway_pdp, "_gw_http", gw),
    ]


class _Scenario:
    def __init__(self, gw, chiamante, monetazione=None):
        self._p = _mondo(gw, chiamante, monetazione)

    def __enter__(self):
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()
        return False


class AnAgentIsAuthorizedAsAnAgentTests(unittest.TestCase):

    def test_the_agent_token_is_forwarded_instead_of_a_human_one(self):
        """Il PDP deve vedere l'agente: è l'unico modo perché guardi i suoi
        grant. Coniare un token on-behalf sul suo nome chiede al gateway di
        decidere sul ruolo di un umano che non esiste."""
        gw = _Gateway()
        with _Scenario(gw, _Chiamante("sysadmin", "ai")):
            chi = gateway_pdp.require_authz(_richiesta(), "packs.setup_done")
        self.assertEqual(chi, "sysadmin")
        self.assertEqual(gw.bearer, _TOKEN)
        self.assertEqual(gw.chiamate[-1]["json"], {"tool": "packs.setup_done"})

    def test_a_refusal_does_not_blame_the_admin_role(self):
        """«azione riservata agli admin» a un agente manda a cercare un problema
        che non c'è: il messaggio deve nominare il verbo e il principal."""
        gw = _Gateway(allowed=False)
        with _Scenario(gw, _Chiamante("sysadmin", "ai")):
            with self.assertRaises(Exception) as e:
                gateway_pdp.require_authz(_richiesta(), "packs.setup_done")
        msg = str(getattr(e.exception, "detail", e.exception))
        self.assertIn("packs.setup_done", msg)
        self.assertIn("sysadmin", msg)
        self.assertNotIn("admin", msg.replace("sysadmin", ""))


class TheHumanBranchIsUnchangedTests(unittest.TestCase):
    """L'eccesso di zelo è il difetto successivo: la webui non ha un token
    d'agente da inoltrare, e per lei il token on-behalf va coniato come prima."""

    def test_a_human_session_still_gets_an_on_behalf_token(self):
        gw, coniati = _Gateway(), []
        with _Scenario(gw, _Chiamante("davide", "human"), monetazione=coniati):
            chi = gateway_pdp.require_authz(_richiesta("ckt1.sessione-webui"),
                                            "packs.import_url")
        self.assertEqual(chi, "davide")
        self.assertEqual(len(coniati), 1, "il token umano deve essere coniato")
        self.assertEqual(gw.bearer, "ckt1.coniato-on-behalf")

    def test_an_unknown_principal_is_treated_as_a_human(self):
        """Chi non è nella registry non è un agente: i principal umani non ci
        sono tutti, e trattarli da agenti inoltrerebbe un token che il gateway
        non sa mappare su nessuna matrice."""
        gw, coniati = _Gateway(), []
        with _Scenario(gw, None, monetazione=coniati), \
             patch.object(gateway_pdp.pki, "verify_session_token",
                          lambda _t: {"agent": "ospite"}):
            gateway_pdp.require_authz(_richiesta(), "packs.import_url")
        self.assertEqual(len(coniati), 1)


class TheRouteAsksForTheVerbItImplementsTests(unittest.TestCase):

    def test_setup_done_asks_for_packs_setup_done(self):
        chiesti: list[str] = []

        async def _authz(_req, tool):
            chiesti.append(tool)
            return "sysadmin"

        with patch.object(packs.gateway_pdp, "require_authz_async", _authz), \
             patch.object(packs, "set_setup_pending", lambda *a, **k: None):
            out = asyncio.run(packs.mark_pack_setup_done("studio-legale",
                                                         _richiesta()))
        self.assertEqual(chiesti, ["packs.setup_done"])
        self.assertEqual(out, {"name": "studio-legale", "setup_pending": False})


if __name__ == "__main__":
    unittest.main()
