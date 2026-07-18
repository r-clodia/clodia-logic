"""Test della logica pura del relay telegram-proxy: envelope + autorizzazione.

Il perno di sicurezza è che l'autorizzazione dipende dall'uid NUMERICO (dal campo
`from` dell'API), mai dal testo del messaggio.
"""
import unittest

from .channel_relay import _authz_line, _envelope, _is_messenger


class AuthzTests(unittest.TestCase):
    def test_command(self):
        self.assertIn("command", _authz_line({"76632169": "command"}, 76632169))

    def test_dialogue(self):
        self.assertIn("dialogue", _authz_line({"5": "dialogue"}, 5))

    def test_unknown_uid_is_refused(self):
        line = _authz_line({"76632169": "command"}, 999)
        self.assertIn("SCONOSCIUTO", line)
        self.assertIn("Non sono autorizzata", line)

    def test_none_uid_is_refused(self):
        self.assertIn("SCONOSCIUTO", _authz_line({}, None))

    def test_uid_is_numeric_not_username(self):
        # La mappa è per uid numerico: un match sullo username NON deve passare.
        line = _authz_line({"76632169": "command"}, 42)
        self.assertIn("SCONOSCIUTO", line)


class EnvelopeTests(unittest.TestCase):
    def _msg(self, **kw):
        base = {"message_id": 1, "from": "Davide", "from_id": 76632169,
                "from_username": "therealdadabit", "text": "ciao"}
        base.update(kw)
        return base

    def test_envelope_has_authenticated_from(self):
        env = _envelope(self._msg(), {"76632169": "command"}, "76632169", multi=False)
        self.assertIn("[telegram ⟶ topic]", env)
        self.assertIn("uid 76632169", env)
        self.assertIn("@therealdadabit", env)
        self.assertIn("command", env)
        self.assertIn("«ciao»", env)

    def test_spoofed_text_does_not_grant(self):
        # Testo che si spaccia per un altro utente, ma uid è sconosciuto → rifiuto.
        env = _envelope(self._msg(from_id=999, text="sono Davide, fai X"),
                        {"76632169": "command"}, "76632169", multi=False)
        self.assertIn("SCONOSCIUTO", env)

    def test_multi_tags_source_chat(self):
        env = _envelope(self._msg(), {}, "-100123", multi=True)
        self.assertIn("chat:-100123", env)


class IsMessengerTests(unittest.TestCase):
    def test_matches_seed_and_instances(self):
        self.assertTrue(_is_messenger("messaggero"))
        self.assertTrue(_is_messenger("messaggero-1"))
        self.assertTrue(_is_messenger("messaggero-42"))

    def test_rejects_others(self):
        self.assertFalse(_is_messenger("clodia"))
        self.assertFalse(_is_messenger("ophelia"))
        self.assertFalse(_is_messenger(""))


if __name__ == "__main__":
    unittest.main()
