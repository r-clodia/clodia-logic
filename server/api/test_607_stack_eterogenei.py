"""Stack eterogenei: «meno costoso» smette di voler dire «uguale ma più economico».

`effective_provider_for_tier` sceglie il provider ATTIVO meno costoso fra gli
idonei. La regola nasce quando tutti i provider di un agente servivano lo STESSO
modello: lì il costo era l'unica differenza, e prendere il minimo era gratis.

Con gli stack cross-SDK (clodia-platform#93) un seed può dichiarare modelli
DIVERSI per provider — ed è il caso del segretario di #607: Sonnet 5.5 sui
provider Anthropic, `gemma-4-26b-a4b-it` su Scaleway come unico ripiego SEAL-3.
Scaleway ha `cost_rank: 5`, i provider Anthropic 10-20: col minimo sul costo il
segretario girerebbe su gemma in OGNI stanza e su Sonnet in nessuna. Qui «più
economico» significa «meno capace», cioè il contrario di una preferenza.

Invariante: **se gli stack dichiarano modelli diversi per provider, vince
l'ordine di preferenza del seed; se il modello è unico, resta il minimo sul
costo** — cioè ogni altro seed di oggi non cambia comportamento.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import providers as P


def _no_pause():
    return patch.object(P, "_load_paused", lambda: set())


#: Lo stack del segretario di #607, nella forma derivata che il runtime vede
#: (`models._normalize_stacks`: `model` = primario, `provider_models` = gli
#: scostamenti). Scritto qui una volta sola perché i casi sotto parlino dello
#: stesso agente.
SEGRETARIO_PROVIDERS = ["claude-team", "claude-pro-max", "anthropic-api",
                        "aws-region-eu", "scaleway"]
SEGRETARIO_MODEL = "claude-sonnet-5-5"
SEGRETARIO_PROVIDER_MODELS = {"scaleway": "gemma-4-26b-a4b-it"}


class StackEterogeneiTests(unittest.TestCase):
    def test_tier_basso_non_ripiega_sul_modello_piu_debole(self) -> None:
        """SEAL-1: Scaleway è idoneo e costa meno, ma serve un ALTRO modello."""
        with _no_pause():
            eff = P.effective_provider_for_tier(
                SEGRETARIO_PROVIDERS, None, "claude", set(SEGRETARIO_PROVIDERS),
                "SEAL-1", SEGRETARIO_MODEL, SEGRETARIO_PROVIDER_MODELS)
        self.assertEqual(eff, "claude-team")

    def test_preferenza_rispettata_anche_saltando_il_primo(self) -> None:
        """Il primo della lista non è collegato: vince il secondo DICHIARATO,
        non il più economico fra i rimasti."""
        with _no_pause():
            eff = P.effective_provider_for_tier(
                SEGRETARIO_PROVIDERS, None, "claude",
                {"claude-pro-max", "anthropic-api", "scaleway"},
                "SEAL-1", SEGRETARIO_MODEL, SEGRETARIO_PROVIDER_MODELS)
        self.assertEqual(eff, "claude-pro-max")

    def test_tier_alto_scende_sull_unico_idoneo(self) -> None:
        """SEAL-3: i provider Anthropic non reggono il tier, resta Scaleway —
        la preferenza non è un modo per sforare il tier."""
        with _no_pause():
            eff = P.effective_provider_for_tier(
                SEGRETARIO_PROVIDERS, None, "claude", set(SEGRETARIO_PROVIDERS),
                "SEAL-3", SEGRETARIO_MODEL, SEGRETARIO_PROVIDER_MODELS)
        self.assertEqual(eff, "scaleway")

    def test_modello_unico_resta_min_costo(self) -> None:
        """Nessuno scostamento per provider ⇒ comportamento di prima, intatto:
        fra due provider che servono lo stesso modello vince il meno costoso,
        anche quando è il SECONDO della preferenza dichiarata."""
        with _no_pause():
            eff = P.effective_provider_for_tier(
                ["claude-pro-max", "anthropic-api"], None, "claude",
                {"claude-pro-max", "anthropic-api"}, "SEAL-1", "claude-sonnet-5-5")
        self.assertEqual(eff, "anthropic-api")

    def test_scostamento_su_un_provider_non_idoneo_non_conta(self) -> None:
        """L'eterogeneità si misura sui CANDIDATI idonei, non sulla dichiarazione:
        se il provider col modello diverso non regge il tier, i rimasti servono
        tutti lo stesso modello e il minimo sul costo resta la regola giusta."""
        with _no_pause():
            eff = P.effective_provider_for_tier(
                SEGRETARIO_PROVIDERS, None, "claude",
                {"claude-pro-max", "anthropic-api"},
                "SEAL-1", SEGRETARIO_MODEL, SEGRETARIO_PROVIDER_MODELS)
        self.assertEqual(eff, "anthropic-api")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
