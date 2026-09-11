"""Un provider può servire «gpt-* tranne i -codex», e il catalogo sa dirlo.

clodia-platform#333 — punto 3 («un controllo di compatibilità
model↔provider-mechanism prima di lanciare il turno»).

## Il turno perso

`ophelia` (`agent_sdk: codex`, `model: gpt-5-codex`, nessun `providers:`
dichiarato → default dell'SDK: `openai-api` poi `codex`) ha perso un turno su
`SEAL-1/audit-leadgen-davide` con un 400 **pulito** dal backend:

    The 'gpt-5-codex' model is not supported when using Codex with a ChatGPT account.

Non un crash, non un timeout: OpenAI ha risposto, e ha risposto di no. Il
meccanismo `subscription` (provider `codex`, oauth su account ChatGPT) non
abilita i modelli della famiglia `-codex`; il meccanismo `apikey` (provider
`openai-api`) sì. Il ripiego automatico era caduto sull'abbonamento.

## Perché la piattaforma l'ha lasciato passare

La macchina per impedirlo c'era già tutta. `provider_supports_model()` è il
punto condiviso da cui passano i tre percorsi che possono accoppiare un agente a
un provider:

  - `candidate_providers()` — il ripiego automatico, cioè il caso di questa issue;
  - `session._ensure_runtime_provider()` — il fail-fast prima del turno;
  - `agent_registry` — il 400 sull'override manuale dal profilo.

Mancava il **modo di dire la restrizione**. Il catalogo esprimeva solo globs
POSITIVI, e `providers/codex.yaml` dichiara `models: ["gpt-*", …]`: `gpt-*`
cattura anche `gpt-5-codex`. «`gpt-*` tranne i `-codex`» non era scrivibile,
quindi il catalogo affermava una cosa falsa e nessuno dei tre percorsi poteva
accorgersene.

## La forma della correzione

`models_excluded` accanto a `models`, e le esclusioni VINCONO sulle inclusioni.
La restrizione resta un **dato del catalogo**, non conoscenza di OpenAI cablata
dentro il motore di routing: un `if mechanism == "subscription" and "-codex" in
model` sarebbe stato lo stesso fatto scritto nel posto dove il prossimo provider
lo troverà sbagliato. È la stessa direzione di `model_ids` per Bedrock, che pure
poteva essere una regola e invece è una mappa.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from . import providers as P


class L_abbonamento_ChatGPT_non_serve_i_modelli_codex(unittest.TestCase):
    """Il caso misurato: il 400 che ha bruciato il turno di `ophelia-8`."""

    def test_il_modello_rifiutato_non_risulta_piu_servito(self) -> None:
        self.assertFalse(P.provider_supports_model("codex", "gpt-5-codex"))

    def test_ma_l_apikey_lo_serve_ancora(self) -> None:
        """`openai-api` è il meccanismo che quel modello lo ha sempre accettato:
        se la correzione lo togliesse anche di lì, `ophelia` resterebbe senza
        nessun provider — avremmo spento l'agente per chiudere l'incidente."""
        self.assertTrue(P.provider_supports_model("openai-api", "gpt-5-codex"))

    def test_la_restrizione_non_si_allarga_agli_altri_gpt(self) -> None:
        """Il 400 nomina UNA famiglia. Escludere `gpt-*` in blocco toglierebbe
        l'abbonamento a ogni agente codex: sarebbe una decisione di prodotto
        travestita da correzione di bug."""
        for m in ("gpt-5", "gpt-5.5", "gpt-5.6-sol", "o3", "o4-mini"):
            with self.subTest(model=m):
                self.assertTrue(P.provider_supports_model("codex", m))

    def test_il_ripiego_automatico_non_propone_piu_l_abbonamento(self) -> None:
        """LA RIPRODUZIONE. `ophelia` non dichiara `providers:`, quindi eredita
        il default dell'SDK codex: `openai-api` (priority 10) poi `codex` (20).
        Era il secondo a raccogliere il turno quando il primo non era spendibile.
        """
        self.assertEqual(
            ["openai-api"],
            P.candidate_providers(None, None, "codex", "gpt-5-codex"))

    def test_e_per_un_modello_non_ristretto_l_abbonamento_resta(self) -> None:
        self.assertEqual(
            ["openai-api", "codex"],
            P.candidate_providers(None, None, "codex", "gpt-5"))


class Il_vincolo_e_un_dato_non_codice(unittest.TestCase):

    def test_lo_dichiara_il_file_del_provider(self) -> None:
        """Se un domani OpenAI togliesse la restrizione, questa deve essere una
        riga di YAML da cancellare — non una condizione da ritrovare in
        `providers.py`."""
        d = yaml.safe_load(
            (Path(P.PROVIDERS_DEF_DIR) / "codex.yaml").read_text(encoding="utf-8"))
        self.assertTrue(d.get("models_excluded"),
                        "codex.yaml non dichiara `models_excluded`")

    def test_nessun_nome_di_modello_finisce_nel_motore_di_routing(self) -> None:
        """Un `if "-codex" in model` dentro `providers.py` sarebbe lo stesso
        fatto, scritto nel posto in cui il prossimo provider lo troverà
        sbagliato.

        Si guarda il CODICE, non i commenti: spiegare perché il ramo esiste —
        citando il 400 che l'ha causato — è documentazione, e toglierla per far
        passare una guard renderebbe il file meno leggibile senza renderlo più
        corretto. Commenti e stringhe si scartano con `tokenize`.
        """
        import io
        import tokenize

        src = (Path(__file__).parent / "providers.py").read_text(encoding="utf-8")
        codice = " ".join(
            t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING))
        for nome in ("gpt-5-codex", "-codex", "gpt-"):
            with self.subTest(nome=nome):
                self.assertNotIn(nome, codice,
                                 "il vincolo di UN provider è finito nel codice "
                                 "che li serve tutti")


class Il_meccanismo_in_generale(unittest.TestCase):
    """`models_excluded` non è un caso speciale per OpenAI: è una chiave del
    catalogo, e va provata come tale su un provider sintetico."""

    def _catalogo(self, voce: dict):
        return patch.dict(P._CATALOG, {"finto": voce}, clear=False)

    def test_l_esclusione_vince_sull_inclusione(self) -> None:
        with self._catalogo({"models": ["gpt-*"], "models_excluded": ["*-codex"]}):
            self.assertFalse(P.provider_supports_model("finto", "gpt-5-codex"))
            self.assertTrue(P.provider_supports_model("finto", "gpt-5"))

    def test_vale_anche_senza_lista_positiva(self) -> None:
        """`models` vuoto significa «qualunque modello del suo SDK»: un'eccezione
        dentro un insieme aperto resta un'eccezione."""
        with self._catalogo({"models": [], "models_excluded": ["*-codex"]}):
            self.assertFalse(P.provider_supports_model("finto", "gpt-5-codex"))
            self.assertTrue(P.provider_supports_model("finto", "qualunque-cosa"))

    def test_un_provider_senza_esclusioni_non_cambia(self) -> None:
        with self._catalogo({"models": ["claude-*"]}):
            self.assertTrue(P.provider_supports_model("finto", "claude-opus-5"))
            self.assertFalse(P.provider_supports_model("finto", "gpt-5"))

    def test_modello_ignoto_nessun_vincolo(self) -> None:
        """`model=None` = «non si sta chiedendo di un modello»: un'esclusione non
        deve trasformare l'assenza di domanda in un rifiuto."""
        with self._catalogo({"models_excluded": ["*"]}):
            self.assertTrue(P.provider_supports_model("finto", None))


if __name__ == "__main__":
    unittest.main()
