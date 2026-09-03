"""Su Bedrock la VERSIONE dichiarata da un seed è quella che gira.

Il difetto, trovato da Davide il 3 set 2026: `bedrock_model_id()` guardava solo
la FAMIGLIA del modello

    if "opus" in m: return env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")

quindi `claude-opus-5`, `claude-opus-4-8` e `claude-opus-4-7` finivano TUTTI su
`eu.anthropic.claude-opus-4-6-v1`. In silenzio: nessun log, nessun errore, e
`GET /api/agents` mostrava il modello del seed — cioè la configurazione, non ciò
che gira. `avvocato` è stato portato a `claude-opus-5` il 2 set e ha continuato a
girare su Opus 4.6 per un giorno, con una conferma sbagliata data per buona.
I profili delle altre versioni erano in `eu-west-1` da sempre (verificato con
ListInferenceProfiles: opus 4-5/4-6/4-7/4-8/5, sonnet 4-6/5).

Perché una guard e non un commento: la regressione è MUTA in senso pieno. Non
rompe un turno, non fallisce un tipo, non lascia una riga nei log — restituisce
un modello funzionante che non è quello chiesto, e il posto dove si andrebbe a
guardare (il registry) risponde con la configurazione, confermando l'errore.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import providers as P


#: Il provider di prova replica la FORMA di providers/aws-region-eu.yaml: env di
#: famiglia + mappa esplicita per versione. I valori sono quelli veri, così se il
#: file cambia forma questo test lo dice.
_FINTO = {
    "aws-eu-test": {
        "name": "Bedrock di prova",
        "sdk": "claude",
        "extra_env": {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "eu-west-1",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "eu.anthropic.claude-opus-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "eu.anthropic.claude-sonnet-5",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        },
        "model_ids": {
            "claude-opus-5": "eu.anthropic.claude-opus-5",
            "claude-opus-4-8": "eu.anthropic.claude-opus-4-8",
            "claude-opus-4-6": "eu.anthropic.claude-opus-4-6-v1",
            "claude-sonnet-5": "eu.anthropic.claude-sonnet-5",
            "claude-sonnet-4-6": "eu.anthropic.claude-sonnet-4-6",
        },
    },
    "non-bedrock": {"name": "API diretta", "sdk": "claude", "extra_env": {}},
}


class BedrockModelVersionTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict(P._CATALOG, _FINTO, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def _id(self, model, pid="aws-eu-test"):
        return P.bedrock_model_id(pid, model)

    # --- il cuore del difetto ----------------------------------------------
    def test_versioni_diverse_danno_profili_diversi(self):
        """IL CASO DELLA SEGNALAZIONE: due seed opus con versioni diverse non
        devono finire sullo stesso profilo."""
        a = self._id("claude-opus-5")
        b = self._id("claude-opus-4-8")
        self.assertEqual(a, "eu.anthropic.claude-opus-5")
        self.assertEqual(b, "eu.anthropic.claude-opus-4-8")
        self.assertNotEqual(a, b)

    def test_opus_5_non_diventa_opus_4_6(self):
        self.assertEqual(self._id("claude-opus-5"), "eu.anthropic.claude-opus-5")

    def test_sonnet_rispetta_la_versione(self):
        self.assertEqual(self._id("claude-sonnet-5"), "eu.anthropic.claude-sonnet-5")
        self.assertEqual(self._id("claude-sonnet-4-6"), "eu.anthropic.claude-sonnet-4-6")

    def test_il_suffisso_irregolare_viene_dalla_mappa(self):
        """`claude-opus-4-6` è `…-4-6-v1`: è la ragione per cui la traduzione è
        una mappa e non la regola `eu.` + nome."""
        self.assertEqual(self._id("claude-opus-4-6"), "eu.anthropic.claude-opus-4-6-v1")

    # --- ripieghi, ma dichiarati ------------------------------------------
    def test_modello_ignoto_ripiega_sulla_famiglia_e_lo_dice(self):
        with self.assertLogs("agent-server.api.providers", level="WARNING") as log:
            got = self._id("claude-opus-9-inesistente")
        self.assertEqual(got, "eu.anthropic.claude-opus-5")
        self.assertTrue(any("ripiego" in r.getMessage() for r in log.records), log.output)

    def test_un_modello_in_mappa_non_logga_nulla(self):
        """Il warning deve segnalare il ripiego, non accompagnare il caso
        normale: un log che compare sempre non viene più letto."""
        import logging
        with patch.object(P.LOG, "warning") as w:
            self._id("claude-opus-5")
        w.assert_not_called()
        del logging

    # --- invarianti da non rompere ----------------------------------------
    def test_provider_non_bedrock_non_traduce(self):
        self.assertIsNone(self._id("claude-opus-5", pid="non-bedrock"))

    def test_un_id_bedrock_non_viene_tradotto_due_volte(self):
        """Il runtime override porta la forma GIÀ tradotta: ritradurla la
        cercherebbe in `model_ids` (dove non c'è) e la manderebbe sul default,
        cioè un downgrade silenzioso al secondo passaggio."""
        for gia in ("eu.anthropic.claude-opus-4-8", "us.anthropic.claude-opus-5",
                    "global.anthropic.claude-sonnet-4-6"):
            self.assertEqual(self._id(gia), gia)

    def test_modello_vuoto_o_assente(self):
        self.assertIsNone(self._id(None))
        self.assertIsNone(self._id(""))

    def test_maiuscole_non_cambiano_la_risoluzione(self):
        self.assertEqual(self._id("Claude-Opus-5"), "eu.anthropic.claude-opus-5")


class RealProviderFileTests(unittest.TestCase):
    """Il file vero del provider deve dichiarare la mappa e i default aggiornati.

    Senza queste due asserzioni la funzione può essere corretta e la
    configurazione ferma: è esattamente lo stato in cui il difetto è stato
    trovato — codice che traduce e un provider che dichiara un solo profilo per
    famiglia.
    """

    def test_aws_region_eu_dichiara_i_model_ids(self):
        d = P._CATALOG.get("aws-region-eu") or {}
        mappa = d.get("model_ids") or {}
        self.assertIn("claude-opus-5", mappa, "manca la voce per Opus 5")
        self.assertEqual(mappa["claude-opus-5"], "eu.anthropic.claude-opus-5")
        # Le versioni distinte devono puntare a profili distinti.
        self.assertNotEqual(mappa.get("claude-opus-5"), mappa.get("claude-opus-4-6"))

    def test_default_di_famiglia_non_sono_fermi_a_una_versione_vecchia(self):
        env = P.provider_extra_env("aws-region-eu")
        self.assertEqual(env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                         "eu.anthropic.claude-opus-5")


class ContextWindowTests(unittest.TestCase):
    """Opus 5 è 1M-capable: senza la sua voce cadeva nel fallback di famiglia
    (200k) e la barra del contesto della webui mostrava un quinto del vero."""

    def test_opus_5_ha_la_finestra_da_un_milione(self):
        from ..agents import model_context
        self.assertEqual(
            model_context.model_context_window("claude-opus-5", "claude"), 1_000_000)

    def test_una_versione_senza_voce_resta_prudente(self):
        from ..agents import model_context
        self.assertEqual(
            model_context.model_context_window("claude-opus-4-5", "claude"), 200_000)


if __name__ == "__main__":
    unittest.main()
