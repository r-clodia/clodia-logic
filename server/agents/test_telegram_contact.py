"""Il recapito Telegram è un campo, e un campo si valida (clodia-platform#200).

Il difetto misurato: sul principal `davide` il campo era `None` — il contatto
era stato scritto nei campi liberi del profilo PII, che nessun lettore consulta.
Quindi `get_by_telegram` non riconosceva i suoi messaggi in ingresso e l'ultimo
gradino di R4 non aveva dove mandare la notifica: l'owner risultava
raggiungibile e non lo era.

Qui si guarda la parte di quel difetto che vive nello schema. Due forme si
possono scrivere e servono a cose diverse:

  chat_id numerico   identifica CHI scrive e riceve le notifiche;
  @handle            identifica soltanto — `sendMessage` risolve `@nome` per i
                     canali, mai per una persona.

Terza forma non esiste: un link `t.me`, un numero di telefono o un indirizzo
email in quel campo hanno l'aria del recapito e non recapitano niente. È la
distinzione che l'issue chiedeva di decidere, e qui è eseguibile.
"""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from .models import AgentSpec, normalize_telegram, telegram_delivers


def _human(**extra) -> dict:
    return {"name": "davide", "description": "d", "display_name": "Davide",
            "type": "human", "role": "superadmin", **extra}


class NormalizeTests(unittest.TestCase):
    def test_the_numeric_chat_id_passes_untouched(self):
        self.assertEqual("76632169", normalize_telegram(" 76632169 "))

    def test_a_group_id_is_negative_and_still_an_id(self):
        self.assertEqual("-1001234567", normalize_telegram("-1001234567"))

    def test_a_handle_keeps_one_leading_at(self):
        """La forma canonica ne ha esattamente uno: chi scrive `davide_c` e chi
        scrive `@davide_c` ha scritto la stessa cosa, e due varianti dello
        stesso valore sono il primo passo verso due valori."""
        self.assertEqual("@davide_c", normalize_telegram("davide_c"))
        self.assertEqual("@davide_c", normalize_telegram("@davide_c"))

    def test_nothing_declared_stays_nothing(self):
        self.assertIsNone(normalize_telegram(None))
        self.assertIsNone(normalize_telegram("   "))

    def test_what_is_not_a_contact_is_refused(self):
        """Ognuna di queste sembra un recapito a chi la scrive nel campo."""
        for value in ("https://t.me/davide_c", "t.me/davide_c", "+39 333 1234567",
                      "davide@example.com", "Davide (telegram)", "@ab"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_telegram(value)

    def test_the_refusal_says_which_form_delivers(self):
        """Un rifiuto che non nomina la forma buona lascia riprovare a caso."""
        with self.assertRaises(ValueError) as e:
            normalize_telegram("https://t.me/davide_c")
        self.assertIn("chat_id", str(e.exception))
        self.assertIn("handle", str(e.exception))


class DeliversTests(unittest.TestCase):
    """Solo l'id è un destinatario. È la domanda che l'issue chiedeva di
    rendere esplicita: un campo pieno di `@davide` può sembrare corretto e non
    consegnare mai."""

    def test_only_the_numeric_id_delivers(self):
        self.assertTrue(telegram_delivers("76632169"))
        self.assertFalse(telegram_delivers("@davide_c"))
        self.assertFalse(telegram_delivers(None))
        self.assertFalse(telegram_delivers(""))


class SpecTests(unittest.TestCase):
    def test_the_spec_stores_the_canonical_form(self):
        self.assertEqual("@davide_c", AgentSpec.model_validate(
            _human(telegram="davide_c")).telegram)
        self.assertEqual("76632169", AgentSpec.model_validate(
            _human(telegram="76632169")).telegram)

    def test_a_seed_with_a_link_does_not_load(self):
        """Rifiutare il seed è più rumoroso che accettarlo: un valore che non
        recapita, caricato in silenzio, è indistinguibile da uno che funziona
        finché non serve — cioè finché serve davvero."""
        with self.assertRaises(ValidationError):
            AgentSpec.model_validate(_human(telegram="https://t.me/davide_c"))


class ReverseLookupTests(unittest.TestCase):
    """Il lookup in INGRESSO continua a riconoscere entrambe le forme.

    Il relay identifica il mittente con lo username (`from_username`; l'uid solo
    se manca), quindi rifiutare gli handle avrebbe chiuso il riconoscimento
    delle persone che scrivono — la metà del difetto che questa issue apre.
    """

    def setUp(self):
        from .loader import AgentRegistry
        self.reg = AgentRegistry()
        self.reg._agents = {
            "davide": AgentSpec.model_validate(_human(telegram="davide_c")),
            "mara": AgentSpec.model_validate({
                "name": "mara", "description": "m", "display_name": "Mara",
                "type": "human", "telegram": "123456"}),
        }

    def test_a_handle_resolves_however_it_is_written(self):
        for scritto in ("davide_c", "@davide_c", " @Davide_C "):
            with self.subTest(scritto=scritto):
                self.assertEqual("davide", self.reg.get_by_telegram(scritto).name)

    def test_a_chat_id_resolves(self):
        self.assertEqual("mara", self.reg.get_by_telegram(" 123456 ").name)

    def test_a_stranger_stays_a_stranger(self):
        self.assertIsNone(self.reg.get_by_telegram("@qualcunaltro"))


class ContactChannelsTests(unittest.TestCase):
    """Chi mostra il contatto e chi conta sulla notifica leggono la stessa
    riga: `telegram_delivers`. Senza, ogni lettore rideduce la regola per conto
    suo, ed è così che una scheda dice «raggiungibile» mentre R4 tace."""

    def _channels(self, **extra):
        from ..api import contacts
        return contacts.channels(AgentSpec.model_validate(_human(**extra)))

    def test_an_id_is_reachable(self):
        ch = self._channels(telegram="76632169")
        self.assertEqual("76632169", ch["telegram"])
        self.assertTrue(ch["telegram_delivers"])

    def test_a_handle_is_declared_not_deliverable(self):
        ch = self._channels(telegram="@davide_c")
        self.assertEqual("@davide_c", ch["telegram"])
        self.assertFalse(ch["telegram_delivers"])

    def test_a_person_without_telegram_is_unreachable(self):
        ch = self._channels()
        self.assertIsNone(ch["telegram"])
        self.assertFalse(ch["telegram_delivers"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
