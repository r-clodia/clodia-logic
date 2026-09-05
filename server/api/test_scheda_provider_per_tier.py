"""La scheda dell'agente non può dire «provider in uso» al singolare.

Residuo 2 di `agents-notebook` A13 — clodia-platform#306.

`_provider_fields` riempiva la card con `effective_provider(...)`, la variante
**senza tier**: risolve l'ordine di preferenza dichiarato e non sa niente di
nessuna stanza. Due commenti nel codice la chiamavano «provider realmente in uso
ora» e «SEAL del provider a cui l'agent è ATTUALMENTE attribuito».

Non è vero, e non lo è da quando il turno di canale apre la sessione con
`topic_runtime_override`: il provider è il **meno costoso idoneo al tier di
quella stanza**, e la sessione è per `(topic, agente)`. Lo stesso agente gira su
provider diversi in stanze diverse.

È da qui che nasce la premessa della domanda che ha aperto A13 — «ora l'agente è
vincolato ad un unico provider alla volta per tutti gli spawn». Non era vero da
tempo, ma è quello che la card diceva, e la card è l'unico posto dove si guarda.

Stessa classe di #296: un dato mostrato senza il contesto che lo rende falso. Lì
la correzione è stata dire il residuo per campo; qui è dire il provider per
tier.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import agent_registry as ar


class _Spec:
    type = "bot"
    agent_sdk = "claude"
    model = "claude-opus-5"
    provider_models = None
    stacks = None

    def __init__(self, name="avvocato", providers=("anthropic-api", "aws-region-eu")):
        self.name = name
        self.providers = list(providers)
        self.provider = None


#: SEAL dichiarato dai due provider del caso reale: uno economico e non sovrano,
#: uno sovrano e più caro. È la situazione in cui la card mente.
_SEAL = {"anthropic-api": "SEAL-1", "aws-region-eu": "SEAL-3"}
_COSTO = {"anthropic-api": 0, "aws-region-eu": 1}


def _per_tier(providers, provider, sdk, connected, tier, model=None, pm=None):
    """`effective_provider_for_tier` in piccolo: min-costo fra gli idonei."""
    ordine = {"SEAL-0": 0, "SEAL-1": 1, "SEAL-2": 2, "SEAL-3": 3, "SEAL-4": 4}
    idonei = [p for p in (providers or [])
              if p in connected and ordine[_SEAL[p]] >= ordine[str(tier)]]
    return min(idonei, key=lambda p: _COSTO[p]) if idonei else None


class _Card:
    def __enter__(self):
        self._p = [
            patch.object(ar, "effective_provider_for_tier", _per_tier),
            patch.object(ar, "effective_provider",
                         lambda providers, provider, sdk, connected, model=None,
                         override=None, provider_models=None:
                         next((p for p in (providers or []) if p in connected), None)),
            patch.object(ar, "candidate_providers",
                         lambda providers, provider, sdk, model=None, pm=None:
                         list(providers or [])),
            patch.object(ar, "provider_seal", lambda p: _SEAL.get(p)),
            patch.object(ar, "provider_override", lambda n: None),
            patch.object(ar, "provider_paused", lambda p: False),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()
        return False

    def campi(self, spec=None, connected=None):
        return ar._provider_fields(spec or _Spec(),
                                   connected if connected is not None
                                   else {"anthropic-api", "aws-region-eu"})


class LaCardDiceIlProviderPerStanza(unittest.TestCase):

    def test_la_mappa_per_tier_c_e(self) -> None:
        with _Card() as c:
            f = c.campi()
        self.assertIn("provider_by_tier", f)
        self.assertEqual(
            {"SEAL-0": "anthropic-api", "SEAL-1": "anthropic-api",
             "SEAL-2": "aws-region-eu", "SEAL-3": "aws-region-eu", "SEAL-4": None},
            f["provider_by_tier"])

    def test_IL_CASO_una_riga_sola_non_bastava(self) -> None:
        """Il campo `provider` da solo dice `anthropic-api`, e in una stanza
        SEAL-2 quell'agente non gira su `anthropic-api`."""
        with _Card() as c:
            f = c.campi()
        self.assertEqual("anthropic-api", f["provider"])
        self.assertNotEqual(f["provider"], f["provider_by_tier"]["SEAL-2"])

    def test_none_dove_l_agente_non_puo_lavorare(self) -> None:
        """`None` non è un buco: è l'informazione che in quel tier l'agente non
        può prendere turni. Nasconderlo sarebbe la bugia opposta."""
        with _Card() as c:
            f = c.campi()
        self.assertIsNone(f["provider_by_tier"]["SEAL-4"])

    def test_un_provider_scollegato_esce_dalla_mappa(self) -> None:
        with _Card() as c:
            f = c.campi(connected={"anthropic-api"})
        self.assertIsNone(f["provider_by_tier"]["SEAL-2"])
        self.assertEqual("anthropic-api", f["provider_by_tier"]["SEAL-0"])

    def test_il_campo_provider_resta(self) -> None:
        """Non si toglie: è il default fuori da una stanza, e per un agente con
        un solo provider idoneo resta anche l'unica risposta. Toglierlo
        romperebbe la card per guadagnare precisione che la mappa dà già."""
        with _Card() as c:
            f = c.campi()
        for campo in ("provider", "provider_seal", "providers", "provider_options"):
            self.assertIn(campo, f)

    def test_un_umano_non_ha_ne_provider_ne_mappa(self) -> None:
        spec = _Spec()
        spec.type = "human"
        with _Card() as c:
            f = c.campi(spec=spec)
        self.assertIsNone(f["provider"])
        self.assertNotIn("provider_by_tier", f)

    def test_tutti_e_cinque_i_tier(self) -> None:
        """Se domani nasce un SEAL-5 la mappa deve seguirlo da sé: si itera la
        costante, non una lista scritta a mano."""
        with _Card() as c:
            f = c.campi()
        self.assertEqual(list(ar._CLR_VALID), list(f["provider_by_tier"]))


class LaCardNonSiRompeSuUnTierChePiantaTutto(unittest.TestCase):

    def test_un_errore_su_un_tier_non_fa_cadere_la_scheda(self) -> None:
        """Una card che non si apre è peggio di una card imprecisa: si perde
        anche tutto il resto del profilo."""
        def _esplode(*a, **k):
            raise RuntimeError("catalogo provider illeggibile")

        with _Card() as c, patch.object(ar, "effective_provider_for_tier", _esplode):
            f = c.campi()
        self.assertEqual({t: None for t in ar._CLR_VALID}, f["provider_by_tier"])


if __name__ == "__main__":
    unittest.main()
