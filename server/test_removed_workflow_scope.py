"""Lo scope `workflow` non c'è più, e questo file è la prova che non torna.

Speculare a `server/test_removed_namespaces.py` di `clodia-tools`: là i verbi
`workflows.*` sono caduti dal gateway, qui cade tutto ciò che da questo lato li
nominava ancora — la superficie HMAC che firmava i gate di un run, il mandato
che il seed di `sysadmin` leggeva a ogni turno, la rotta `/workflows` nella
mappa della WebUI.

Senza questo test la rimozione resta non verificabile, ed è esattamente il
difetto che il file gemello esiste per evitare: il verbo non c'è più, quindi
nessuno lo chiama e nessuno se ne accorge, ma la voce resta scritta e continua
a dire qualcosa di falso sulla superficie di controllo.

**Cosa NON cerca, di proposito.** Il criterio è lo stesso del gemello —
l'occorrenza del namespace (`workflows.`) o del suo nome quotato
(`"workflows"`) — e non la parola «workflow» in libertà. Tre cose restano
legittime e non devono far fallire nulla:

- lo scope `workflow` di un **PAT GitHub** (`.github/workflows/`), che non ha
  alcuna relazione col nostro engine;
- il tool nativo **`Workflow`** della CLI Anthropic, fotografato in
  `sdk_runtime/native_tools.py` e nelle allowlist dei seed: è di un altro
  fornitore, e toglierlo dai seed è una decisione di catalogo (immutable →
  rebuild), non questa pulizia;
- la chiave `"workflow"` nel payload di `/gate`, che è il **contratto** verso
  la pagina pubblica: è un nome, non un motore, e non si rinomina da qui un
  campo che legge un consumatore esterno.
"""
from __future__ import annotations

import pathlib
import unittest

RADICE = pathlib.Path(__file__).parent
CATALOGHI = RADICE.parent / "catalogs"

#: Il namespace dei verbi, come lo scriveva chi lo usava.
RESIDUI = ("workflows.", '"workflows"', "'workflows'")

#: File che parlano della rimozione invece di implementarla: la memoria di
#: perché una cosa non c'è più è utile e non è un residuo.
ESENTI = {"test_removed_workflow_scope.py", "CHANGELOG.md"}


def _sorgenti():
    for radice, pattern in ((RADICE, "*.py"), (CATALOGHI, "*.md"),
                            (CATALOGHI, "*.yaml")):
        for f in radice.rglob(pattern):
            if f.name in ESENTI or "__pycache__" in f.parts:
                continue
            yield f


class NoWorkflowNamespaceTests(unittest.TestCase):
    def test_no_source_mentions_the_namespace(self):
        """Il conto su tutto l'albero, non l'ispezione di un punto: è
        l'aritmetica ad aver fatto danno finora — si convertono venti occorrenze
        su ventuno e la ventunesima continua a lavorare."""
        colpevoli = []
        for f in _sorgenti():
            testo = f.read_text(errors="ignore").lower()
            for residuo in RESIDUI:
                if residuo in testo:
                    colpevoli.append(f"{f.relative_to(RADICE.parent)}:{residuo}")
        self.assertEqual(colpevoli, [], f"residui: {colpevoli}")

    def test_no_seed_promises_the_capability(self):
        """Il pezzo che morde di più: un seed che elenca un mandato inesistente
        lo rilegge a ogni turno, e l'agente ci prova."""
        seed = CATALOGHI / "packs/base-pack/agents/sysadmin/system-prompt.md"
        testo = seed.read_text(errors="ignore").lower()
        self.assertNotIn("workflows.", testo)
        self.assertNotIn("/workflows", testo, "rotta WebUI che non esiste più")


class NoSignedGateForRunsTests(unittest.TestCase):
    def test_no_token_can_be_minted_for_a_run(self):
        """Firmare token validi per i gate di un run che nessuno può più creare
        è superficie HMAC viva su un tipo che non esiste: `make`/`verify` non
        avevano più un solo chiamante, restavano solo pronte all'uso."""
        from .api import gate_sign
        for morto in ("make", "verify", "token_kind"):
            with self.subTest(morto):
                self.assertFalse(hasattr(gate_sign, morto))

    def test_the_job_gate_still_works(self):
        """La controprova: togliendo il ramo run, le proposte di job devono
        continuare a firmare, verificare e rifiutare un token manomesso."""
        from .api import gate_sign
        # Chiave iniettata: il test non deve né leggere né creare la chiave
        # dell'istanza, che è un segreto e non una fixture.
        self.addCleanup(setattr, gate_sign, "_KEY_CACHE", gate_sign._KEY_CACHE)
        gate_sign._KEY_CACHE = b"chiave-di-test-32-byte-esatti!!!"
        nonce = gate_sign.new_nonce()
        token = gate_sign.make_job(42, nonce)
        self.assertEqual(gate_sign.verify_job(token), {"job": 42, "nonce": nonce})
        self.assertIsNone(gate_sign.verify_job(token[:-1] + ("0" if token[-1] != "0" else "1")))


if __name__ == "__main__":
    unittest.main()
