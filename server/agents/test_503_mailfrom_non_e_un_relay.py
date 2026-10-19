"""`mailfrom:` non è un relay (clodia-platform#503).

Il difetto era **testuale**: il mandato del messaggero diceva che un mittente
Telegram autorizzato è un ingress «esattamente come un mittente email», e chi lo
leggeva concludeva che scrivere `mailfrom:<indirizzo>` negli ingress facesse
arrivare la posta di quel mittente. Non è così: `mailfrom:` decide se il
contenuto è fidato (taint), e nei canali a ingresso stretto chi passa.

Dal 19 ott 2026 (clodia-platform#531) il messaggero non esiste più e la posta
ARRIVA davvero nelle stanze, portata dal corriere: ma la porta è `inbox:<casella>`,
non `mailfrom:`. La spiegazione vive nella skill `check-email`, che è ciò che
l'agente legge quando parla di posta; il controllo resta sul TESTO, perché se la
frase sbagliata torna non fallisce nient'altro.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from ..config import workspace_path

SKILL = Path(workspace_path(
    "catalogs/packs/comms-pack/plugins/comms-pack/skills/check-email/SKILL.md"))

#: L'equivalenza che aveva prodotto la promessa sbagliata.
EQUIVALENZA = "esattamente come un mittente email"


class MailfromNonEUnRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.testo = SKILL.read_text(encoding="utf-8")

    def sezione(self) -> str:
        pezzi = self.testo.split("\n## Come arriva la posta", 1)
        self.assertEqual(2, len(pezzi), "sezione «Come arriva la posta» sparita")
        return pezzi[1].split("\n## ", 1)[0]

    def test_non_equipara_la_posta_a_telegram(self) -> None:
        self.assertNotIn(EQUIVALENZA, self.testo)

    def test_distingue_le_due_voci(self) -> None:
        sezione = self.sezione()
        self.assertIn("`inbox:<casella>`", sezione)
        self.assertIn("`mailfrom:<indirizzo>` **non fa arrivare la posta**", sezione)
        self.assertIn("non fidato", sezione)

    def test_nomina_l_eccezione_dei_canali_stretti(self) -> None:
        self.assertIn("ingresso stretto", self.sezione())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
