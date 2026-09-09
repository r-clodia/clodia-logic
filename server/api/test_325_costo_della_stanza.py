"""La squadra proposta per una stanza va prezzata sullo stack DI QUELLA STANZA
(clodia-platform#325, coda di #306 e #315).

`suggest_team(tier, ...)` il tier ce l'ha: è il primo argomento. Ma `_agent_cost`
stimava la fascia di prezzo con `agent_effective_model`/`agent_effective_provider`
— le varianti SENZA tier, cioè l'ordine di preferenza dichiarato, che non sa
niente di nessuna stanza. Per un agente con più stack la proposta per un canale
SEAL-2 veniva quindi valutata sul modello di un provider che in SEAL-2 non può
nemmeno prendere turni, e l'ordinamento «a parità di rilevanza il più economico»
usava il prezzo sbagliato.

Il rimedio non è una nuova risoluzione: `agent_effective_provider_for_tier`
esisteva già, e il modello abbinato lo dà `_runtime_model` — lo stesso helper con
cui il runtime apre le sessioni. Provider e modello si leggono dalla stessa
risposta, come ha stabilito #315.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels
from ..sdk_runtime import session as S


class _Spec:
    type = "bot"
    name = "fullstack-dev"
    model = "claude-sonnet-4-5"
    skills: list[str] = []


# Lo stack cross-tier del caso reale: fino a SEAL-1 il preferito (sonnet,
# «standard»), da SEAL-2 l'unico provider che regge il tier serve opus
# («premium»). Due fasce di prezzo diverse per lo stesso agente.
_PER_TIER = {"SEAL-0": ("anthropic-api", "claude-sonnet-4-5"),
             "SEAL-1": ("anthropic-api", "claude-sonnet-4-5"),
             "SEAL-2": ("aws-region-eu", "claude-opus-5"),
             "SEAL-3": (None, None),
             "SEAL-4": (None, None)}


def _stack_finto():
    """Sostituisce le quattro letture di stack che `_agent_cost` importa."""
    return (
        patch.object(S, "agent_effective_provider", lambda k: "anthropic-api"),
        patch.object(S, "agent_effective_model", lambda k: "claude-sonnet-4-5"),
        patch.object(S, "agent_effective_provider_for_tier",
                     lambda k, t: _PER_TIER.get(t, (None, None))[0]),
        patch.object(S, "agent_effective_model_for_tier",
                     lambda k, t: _PER_TIER.get(t, (None, None))[1]),
    )


class CostoPerStanza(unittest.TestCase):

    def setUp(self) -> None:
        for p in _stack_finto():
            p.start()
            self.addCleanup(p.stop)

    def test_senza_tier_resta_il_preferito(self) -> None:
        """Compatibilità: fuori da una stanza la domanda non ha un tier, e la
        risposta è la stessa di prima."""
        c = channels._agent_cost(_Spec())
        self.assertEqual(c["provider"], "anthropic-api")
        self.assertEqual(c["model"], "claude-sonnet-4-5")
        self.assertEqual(c["label"], "standard")
        self.assertEqual(c["price"], 2)

    def test_il_tier_cambia_la_fascia(self) -> None:
        """IL CASO: in SEAL-2 gira opus, e la squadra costa di più di quanto
        diceva la proposta."""
        c = channels._agent_cost(_Spec(), "SEAL-2")
        self.assertEqual(c["provider"], "aws-region-eu")
        self.assertEqual(c["model"], "claude-opus-5")
        self.assertEqual(c["label"], "premium")
        self.assertEqual(c["price"], 3)

    def test_tier_basso_stesso_esito_del_preferito(self) -> None:
        c = channels._agent_cost(_Spec(), "SEAL-1")
        self.assertEqual(c["provider"], "anthropic-api")
        self.assertEqual(c["label"], "standard")

    def test_tier_precluso_nessun_provider_e_modello_dichiarato(self) -> None:
        """Nessuno stack regge SEAL-4: `provider` dice la verità (`None`) invece
        di nominare uno stack che lì non gira. Il modello ripiega sul dichiarato
        del seed, come già faceva quando il provider non risolveva: serve a dare
        una fascia di prezzo, non a promettere quell'esecuzione — l'idoneità la
        stabilisce `_eligibility`, non il costo."""
        c = channels._agent_cost(_Spec(), "SEAL-4")
        self.assertIsNone(c["provider"])
        self.assertEqual(c["model"], "claude-sonnet-4-5")

    def test_suggest_team_passa_il_tier_al_costo(self) -> None:
        """La catena intera: `suggest_team(tier)` → `_cost_of` → `_agent_cost`.
        Senza questo, il campo per tier esiste e nessuno lo usa."""
        visti = []
        vero = channels._agent_cost

        def spia(spec, tier=None):
            visti.append(tier)
            return vero(spec, tier)

        with patch.object(channels, "_agent_cost", spia), \
                patch.object(channels.registry, "list", lambda: [_Spec()]), \
                patch.object(channels, "_eligibility",
                             lambda s, t: {"eligible": True, "warn": []}), \
                patch.object(channels.responder_routing, "score_specialists",
                             lambda specs, d: [(s, 0.9) for s in specs]):
            out = channels.suggest_team("SEAL-2", "una descrizione")
        self.assertEqual(set(visti), {"SEAL-2"})
        self.assertEqual(out["candidates"][0]["cost"]["label"], "premium")


class ModelloEffettivoUnificato(unittest.TestCase):
    """`agent_effective_model` ricalcolava a mano ciò che `_runtime_model` già
    fa (override per-provider + traduzione inference-profile su Bedrock): due
    copie della stessa regola, e la seconda copia è dove il disallineamento
    ricompare. Questi test fissano l'EQUIVALENZA, cioè che l'unificazione non
    ha cambiato risposta."""

    def test_uguale_a_runtime_model_col_provider_effettivo(self) -> None:
        with patch.object(S, "agent_effective_provider", lambda k: "aws-region-eu"):
            self.assertEqual(S.agent_effective_model("x"),
                             S._runtime_model("x", {"provider": "aws-region-eu"}))

    def test_per_tier_usa_il_provider_del_tier(self) -> None:
        with patch.object(S, "agent_effective_provider_for_tier",
                          lambda k, t: "aws-region-eu"):
            self.assertEqual(S.agent_effective_model_for_tier("x", "SEAL-2"),
                             S._runtime_model("x", {"provider": "aws-region-eu"}))

    def test_per_tier_senza_provider_idoneo_tace(self) -> None:
        """Nessun provider regge il tier → nessun modello. Ripiegare sul
        preferito qui rimetterebbe in circolo esattamente il valore che l'issue
        toglie dalla card."""
        with patch.object(S, "agent_effective_provider_for_tier", lambda k, t: None):
            self.assertIsNone(S.agent_effective_model_for_tier("x", "SEAL-4"))


if __name__ == "__main__":
    unittest.main()
