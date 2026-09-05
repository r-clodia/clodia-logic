"""Il provider si sceglie entrando nella stanza, e la stanza dura più del turno.

Residui 1 e 3 di `agents-notebook` A13 — clodia-platform#305 e #307.

Il requisito («un agente entra in un canale con il provider meno costoso che
rispetta la clearance») era già implementato, ma la scelta si faceva **una volta
sola**, dentro il ramo `except KeyError` che crea la sessione:

    try:
        chat = manager.get(chat_id)      # esistente: nulla veniva ricalcolato
    except KeyError:
        override = topic_runtime_override(spec.name, tier_real)

Una sessione viva teneva quindi per sempre il provider con cui era nata. Due
conseguenze reali: mettere in pausa un provider *sembrava* immediato e sulle
stanze già aperte non lo era; e un topic promosso di tier continuava a girare su
un provider che non lo regge — cioè dati sopra il tier su un provider sotto il
tier, che è precisamente ciò che il tier esiste per impedire.

`_provider_below_tier_warning` esisteva per dirlo e **non veniva mai chiamata**:
era nata per l'eccezione dei super-agent («consentita solo ai super, con popup»,
come la descrive ancora il tipo nel frontend) e `_SUPER_AGENTS` è vuoto dalla
#104. In più costruiva il messaggio con `agent_effective_provider`, la variante
SENZA tier: diceva «il provider in uso» nominando un provider che questa stanza
poteva non aver mai toccato.

Cosa NON si è fatto, ed è una scelta: non si ricalcola quando si connette un
provider più economico. Cambiare provider significa ricreare la sessione, cioè
perdere il contesto della conversazione. Per la sovranità del dato quel prezzo
si paga; per qualche centesimo no.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import channels


class _Chat:
    def __init__(self, provider: str | None):
        self._runtime_override = {"provider": provider} if provider else {}


class _Spec:
    def __init__(self, name="avvocato"):
        self.name = name
        self.clearance = "SEAL-2"


class _Manager:
    """Il manager delle sessioni, sostituito: si misura cosa gli viene chiesto."""

    def __init__(self, chat: _Chat | None):
        self._chat = chat
        self.cancellate: list[str] = []

    def get(self, chat_id):
        if self._chat is None:
            raise KeyError(chat_id)
        return self._chat

    async def delete(self, chat_id):
        self.cancellate.append(chat_id)
        self._chat = None


def _esegui(coro):
    return asyncio.run(coro)


class _Mondo:
    """Contesto: manager, idoneità del provider, sostituto, occupazione."""

    def __init__(self, in_uso: str | None, idoneo: bool, sostituto: str | None = None,
                 occupata: bool = False):
        self.mgr = _Manager(_Chat(in_uso) if in_uso is not None else None)
        self.annunci: list[tuple] = []
        self._idoneo, self._sost, self._busy = idoneo, sostituto, occupata

    def __enter__(self):
        from . import providers
        async def _annuncia(tier, name, spec, pid):
            self.annunci.append((tier, name, spec.name, pid))
        self._p = [
            patch.object(channels, "manager", self.mgr),
            patch.object(providers, "provider_usable_for_tier",
                         lambda pid, tier: self._idoneo),
            patch.object(channels, "_topic_provider", lambda spec, tier: self._sost),
            patch.object(channels, "_chat_busy", lambda cid: self._busy),
            patch.object(channels, "_announce_provider_inadeguato", _annuncia),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()
        return False

    def chiedi(self) -> bool:
        return _esegui(channels._provider_della_stanza_ancora_valido(
            "SEAL-2", "stanza", "SEAL-2", _Spec(), "chan:SEAL-2:stanza:avvocato"))


class UnaSessioneVivaSiRiadegua(unittest.TestCase):

    def test_provider_ancora_idoneo_non_tocca_niente(self) -> None:
        """Il caso normale: non si ricrea una sessione per abitudine."""
        with _Mondo(in_uso="scaleway", idoneo=True) as m:
            self.assertTrue(m.chiedi())
        self.assertEqual([], m.mgr.cancellate)

    def test_provider_diventato_inidoneo_la_sessione_si_rifa(self) -> None:
        """IL CASO DI #305: pausa, disconnessione, o tier del topic alzato."""
        with _Mondo(in_uso="anthropic-api", idoneo=False, sostituto="aws-region-eu") as m:
            self.assertTrue(m.chiedi(), "il turno deve partire, col provider nuovo")
        self.assertEqual(["chan:SEAL-2:stanza:avvocato"], m.mgr.cancellate)

    def test_senza_sostituto_il_turno_non_parte(self) -> None:
        """Proseguire vorrebbe dire trattare dati di questo tier su un provider
        che non lo regge: è esattamente ciò che il tier impedisce."""
        with _Mondo(in_uso="anthropic-api", idoneo=False, sostituto=None) as m:
            self.assertFalse(m.chiedi())
        self.assertEqual([], m.mgr.cancellate, "non si cancella se non c'è alternativa")

    def test_e_lo_dice_nel_canale(self) -> None:
        """Un turno che non parte in silenzio è indistinguibile da un agente
        rotto — e qui la ragione riguarda la sovranità del dato."""
        with _Mondo(in_uso="anthropic-api", idoneo=False, sostituto=None) as m:
            m.chiedi()
        self.assertEqual(1, len(m.annunci))
        self.assertEqual("anthropic-api", m.annunci[0][3],
                         "l'annuncio deve nominare il provider DELLA STANZA")

    def test_sessione_occupata_si_rimanda(self) -> None:
        """Interrompere un turno in corso non lo rende retroattivamente idoneo,
        e ucciderebbe un lavoro a metà."""
        with _Mondo(in_uso="anthropic-api", idoneo=False,
                    sostituto="aws-region-eu", occupata=True) as m:
            self.assertTrue(m.chiedi())
        self.assertEqual([], m.mgr.cancellate)

    def test_sessione_inesistente_lascia_decidere_alla_create(self) -> None:
        with _Mondo(in_uso=None, idoneo=False) as m:
            self.assertTrue(m.chiedi())
        self.assertEqual([], m.mgr.cancellate)


class IlWarningNominaIlProviderGiusto(unittest.TestCase):
    """#307: diceva «il provider in uso» risolvendo la variante senza tier."""

    def test_usa_il_provider_passato(self) -> None:
        w = channels._provider_below_tier_warning(_Spec(), "SEAL-3", "anthropic-api")
        self.assertEqual("anthropic-api", w["provider"])
        self.assertIn("anthropic-api", w["message"])

    def test_la_forma_attesa_dal_frontend_resta(self) -> None:
        """`types.ts` dichiara TierWarning: cambiare le chiavi qui romperebbe una
        UI che non ha ancora mai ricevuto questo avviso."""
        w = channels._provider_below_tier_warning(_Spec(), "SEAL-3", "anthropic-api")
        for campo in ("kind", "tier", "responder", "provider", "provider_seal",
                      "message", "suggestions"):
            self.assertIn(campo, w)
        self.assertEqual("provider_below_tier", w["kind"])

    def test_non_e_piu_codice_morto(self) -> None:
        """Nasceva per l'eccezione dei super-agent, e `_SUPER_AGENTS` è vuoto
        dalla #104: nessuno la chiamava più. Ora la chiama l'annuncio."""
        from pathlib import Path
        src = (Path(__file__).parent / "channels.py").read_text()
        usi = src.count("_provider_below_tier_warning(")
        self.assertGreaterEqual(usi, 2, "definita e mai chiamata: è di nuovo morta")


class EntrambiIPuntiSonoCoperti(unittest.TestCase):
    """I punti che creano una sessione di canale sono DUE.

    Correggerne uno solo ripeterebbe il difetto che il codice qui accanto già
    denuncia: «tre punti che promettono la stessa cosa e uno che non la
    mantiene». La guard è strutturale perché la divergenza non si vede finché
    qualcuno non mette in pausa un provider proprio sulla stanza sbagliata.
    """

    def test_ogni_creazione_di_sessione_e_preceduta_dal_controllo(self) -> None:
        from pathlib import Path
        src = (Path(__file__).parent / "channels.py").read_text()
        creazioni = src.count("override = topic_runtime_override(")
        controlli = src.count("await _provider_della_stanza_ancora_valido(")
        self.assertEqual(creazioni, controlli,
                         f"{creazioni} punti creano una sessione ma {controlli} "
                         "controllano il provider: uno dei due non si riadegua")


if __name__ == "__main__":
    unittest.main()
