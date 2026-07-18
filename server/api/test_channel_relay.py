"""Test della logica pura del relay telegram-proxy (binding istanza↔chat).

Perni: autorizzazione per uid numerico autenticato; il bot risponde solo se
interpellato; il contesto è verbatim con handle autenticati.
"""
import os
import tempfile
import unittest

from .channel_relay import (_addresses_bot, _context_block, _is_messenger, _line,
                            _load_whitelist, _parse_whitelist, _rights, _seed_of)


class RightsTests(unittest.TestCase):
    def test_command_dialogue_unknown(self):
        wl = {"76632169": "command", "5": "dialogue"}
        self.assertEqual(_rights(wl, 76632169), "command")
        self.assertEqual(_rights(wl, 5), "dialogue")
        self.assertIsNone(_rights(wl, 999))
        self.assertIsNone(_rights(wl, None))

    def test_uid_numeric_not_username(self):
        self.assertIsNone(_rights({"76632169": "command"}, 42))


class AddressesBotTests(unittest.TestCase):
    def test_mention_bot(self):
        self.assertTrue(_addresses_bot("ehi @clodia_r_olivay_bot aiutami", []))
        self.assertTrue(_addresses_bot("@Clodia rispondi", []))

    def test_mention_agent_participant(self):
        self.assertTrue(_addresses_bot("@ophelia che ne pensi?", ["ophelia", "davide"]))

    def test_human_chatter_not_addressed(self):
        self.assertFalse(_addresses_bot(
            "ciao @therealdadabit @matlemad ho aggiunto il doc", ["ophelia", "davide"]))


class WhitelistTests(unittest.TestCase):
    def _with_data(self, fn):
        old = os.environ.get("CLODIA_DATA")
        os.environ["CLODIA_DATA"] = tempfile.mkdtemp()
        try:
            return fn(os.environ["CLODIA_DATA"])
        finally:
            if old is None:
                os.environ.pop("CLODIA_DATA", None)
            else:
                os.environ["CLODIA_DATA"] = old

    def test_missing_is_fail_closed(self):
        self.assertEqual(self._with_data(lambda d: _load_whitelist("messaggero")), {})

    def test_block_in_memory_md_primary(self):
        def go(d):
            md = os.path.join(d, "agents", "messaggero", "memory")
            os.makedirs(md)
            open(os.path.join(md, "MEMORY.md"), "w").write(
                "# Memory\n\n<!-- telegram-whitelist -->\n```json\n"
                '{"76632169": "command"}\n```\n')
            return _load_whitelist("messaggero-2")
        self.assertEqual(self._with_data(go), {"76632169": "command"})

    def test_json_fallback(self):
        def go(d):
            md = os.path.join(d, "agents", "messaggero", "memory")
            os.makedirs(md)
            open(os.path.join(md, "telegram_whitelist.json"), "w").write(
                '{"5": "dialogue", "9": "bogus"}')
            return _load_whitelist("messaggero")
        self.assertEqual(self._with_data(go), {"5": "dialogue"})


class ParseWhitelistTests(unittest.TestCase):
    def test_extracts(self):
        md = "x\n<!-- telegram-whitelist -->\n```json\n{\"1\": \"command\"}\n```\ny"
        self.assertEqual(_parse_whitelist(md), {"1": "command"})

    def test_malformed_is_empty(self):
        self.assertEqual(_parse_whitelist("<!-- telegram-whitelist -->\n```json\n{x}\n```"), {})


class SeedTests(unittest.TestCase):
    def test_strips_suffix(self):
        self.assertEqual(_seed_of("messaggero-3"), "messaggero")
        self.assertEqual(_seed_of("messaggero"), "messaggero")


class MessengerTests(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(_is_messenger("messaggero-1"))
        self.assertFalse(_is_messenger("clodia"))


class ContextTests(unittest.TestCase):
    def _m(self, uid, uname, text):
        return {"from_id": uid, "from_username": uname, "from": uname, "text": text}

    def test_line_compact_format(self):
        line = _line(self._m(76632169, "therealdadabit", "ciao"), "-5506202478")
        self.assertEqual(line, "[tg://-5506202478/therealdadabit] -> ciao")

    def test_line_falls_back_to_uid(self):
        line = _line({"from_id": 999, "text": "spam"}, "-5")
        self.assertEqual(line, "[tg://-5/999] -> spam")

    def test_context_block_one_line_per_message(self):
        buffer = [self._m(107393046, "giocasu75", "guardate il doc"),
                  self._m(76632169, "therealdadabit", "@clodia riassumi")]
        block = _context_block(buffer, "-5279916551")
        self.assertEqual(block,
                         "[tg://-5279916551/giocasu75] -> guardate il doc\n"
                         "[tg://-5279916551/therealdadabit] -> @clodia riassumi")


if __name__ == "__main__":
    unittest.main()
