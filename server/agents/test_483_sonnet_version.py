"""Nessun modello dichiarato resta indietro rispetto a quello che il provider serve.

clodia-platform#483 — «upgrade all sonnet based agent seeds with latest 5.5».
La richiesta è una manutenzione ricorrente: esce una versione e qualcuno deve
ricordarsi di toccare N posti scollegati (il seed, il default di famiglia del
provider, la mappa dei profili, la configurazione dello Strategy Agent). Chi ne
dimentica uno non vede rompersi niente — l'agent continua a girare, solo su un
modello vecchio e più caro. È il difetto muto di clodia-platform#392, dove
`avvocato` dichiarava Opus 5 e girava su Opus 4.6 da un giorno senza un log.

L'invariante qui è di COERENZA, non di calendario: il repo non sa qual è
l'ultima release di Anthropic, ma sa qual è la versione che il provider dichiara
di servire di default (`ANTHROPIC_DEFAULT_<FAMIGLIA>_MODEL` in
`providers/aws-region-eu.yaml`, che il suo stesso commento definisce «la
versione più recente disponibile in regione»). Allora nessuno può dichiarare una
versione diversa da quella — né un seed, né `config.yaml`.

**Cosa aggiunge rispetto a `server/api/test_bedrock_model_version.py`.** Là si
controlla che ogni seed su Bedrock abbia la sua voce esplicita in `model_ids`
(cioè che non ripieghi sulla famiglia). Qui si controlla il verso opposto — che
nessuno sia *fermo* a una versione superata — e si guardano anche i modelli
dichiarati in `config.yaml`, che nessun altro test esamina: `colony.strategy`
era rimasto a Sonnet 4.6, due versioni sotto il default del provider, e nessuno
se ne era accorto fino a questa issue.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from ..api import providers as P
from ..config import CONFIG, workspace_path

AGENTS = Path(workspace_path("catalogs/packs/base-pack/agents"))

PROVIDER = "aws-region-eu"

#: Famiglie su cui l'invariante è applicabile: il loro inference-profile EU è
#: esattamente `eu.anthropic.<modello dichiarato>`, quindi dal default si ricava
#: senza ambiguità la forma che un seed deve scrivere. `haiku` resta fuori
#: perché il suo profilo porta data e revisione
#: (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`), che NON è la forma
#: dichiarata da un seed: lì il confronto sarebbe una falsa certezza.
FAMIGLIE = ("opus", "sonnet")

GEO = "eu.anthropic."


def _seeds() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in sorted(p for p in AGENTS.iterdir() if p.is_dir()):
        f = d / "agent.yaml"
        if f.is_file():
            out[d.name] = yaml.safe_load(f.read_text()) or {}
    return out


def _default_dichiarato(famiglia: str) -> str:
    """La forma «da seed» del default di famiglia del provider."""
    profilo = P.provider_extra_env(PROVIDER)[
        f"ANTHROPIC_DEFAULT_{famiglia.upper()}_MODEL"]
    return profilo[len(GEO):] if profilo.startswith(GEO) else profilo


def _famiglia(model: str) -> str | None:
    for f in FAMIGLIE:
        if f in model:
            return f
    return None


def _modelli_di_config() -> dict[str, str]:
    """I modelli dichiarati in `config.yaml`: nessun seed li contiene."""
    out = {"sdk.default_model": str(CONFIG["sdk"]["default_model"])}
    strategia = ((CONFIG.get("colony") or {}).get("strategy") or {}).get("model")
    if strategia:
        out["colony.strategy.model"] = str(strategia)
    return out


def _modelli_dichiarati() -> dict[str, str]:
    """Chi dichiara cosa: i seed del base-pack + `config.yaml`."""
    out = {n: str(s["model"]) for n, s in _seeds().items() if s.get("model")}
    out.update(_modelli_di_config())
    return out


class NessunaVersioneSuperataTests(unittest.TestCase):
    def test_nessuno_dichiara_una_versione_piu_vecchia_del_default(self):
        """Seed e `config.yaml` devono dire la stessa versione del default di
        famiglia del provider. Rosso in entrambi i versi: chi è rimasto fermo a
        una versione vecchia, e chi alza il default senza portarsi dietro il
        resto."""
        visti: set[str] = set()
        for chi, model in _modelli_dichiarati().items():
            fam = _famiglia(model)
            if not fam:
                continue  # non è un Claude di queste famiglie: non ci riguarda
            visti.add(fam)
            with self.subTest(chi=chi, famiglia=fam):
                self.assertEqual(
                    _default_dichiarato(fam), model,
                    f"«{chi}» dichiara {model} mentre {PROVIDER} serve di "
                    f"default {_default_dichiarato(fam)}: una delle due parti "
                    f"è rimasta indietro.")
        # Senza questo, il giorno in cui nessuno dichiara più un Claude il test
        # passerebbe a vuoto senza aver controllato niente.
        self.assertEqual(set(FAMIGLIE), visti,
                         "l'invariante non ha esaminato tutte le famiglie")

    def test_anche_i_modelli_di_config_hanno_il_loro_profilo(self):
        """Gemello per `config.yaml` del controllo che
        `test_bedrock_model_version` fa sui seed: un modello assente da
        `model_ids` ripiega sulla famiglia e gira su un'altra versione."""
        mappa = {str(k).strip().lower()
                 for k in ((P._CATALOG.get(PROVIDER) or {}).get("model_ids") or {})}
        for chi, model in _modelli_di_config().items():
            if not _famiglia(model):
                continue
            with self.subTest(chi=chi, model=model):
                self.assertIn(
                    model.lower(), mappa,
                    f"«{chi}» dichiara {model} ma su {PROVIDER} non c'è la sua "
                    f"voce in model_ids: ripiegherebbe sul default di famiglia.")


if __name__ == "__main__":
    unittest.main()
