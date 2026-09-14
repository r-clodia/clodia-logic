"""Test della logica pura del relay telegram-proxy (binding istanza↔chat).

Perni: l'autorizzazione del mittente è un INGRESS dello scope, chiesta al gateway
e fail-closed (clodia-platform#365); il bot risponde solo se interpellato; il
contesto è verbatim con handle autenticati.
"""
import asyncio
import unittest
from unittest.mock import patch

from . import channel_relay
from .channel_relay import (_addresses_bot, _context_block, _is_messenger,
                            _is_vetted_tg_source, _line)


class _Risposta:
    """La risposta del gateway, quel tanto che ne usa il relay."""

    def __init__(self, payload=None, boom=None):
        self._payload, self._boom = payload or {}, boom

    def raise_for_status(self):
        if self._boom:
            raise self._boom

    def json(self):
        return self._payload


class VettedSourceTests(unittest.TestCase):
    """La domanda «questo mittente è autorizzato?» la fa il GATEWAY.

    Qui si verifica che il relay la ponga nella forma giusta (uri `tg:@handle`,
    direzione ingresso, scope del BINDING) e che non se la risponda da solo
    quando la risposta non arriva.
    """

    def _chiedi(self, username, payload=None, boom=None):
        self.chiamate = []

        def _gw(path, params=None):
            self.chiamate.append((path, params))
            return _Risposta(payload, boom)

        with patch("server.api.observe._gw", _gw):
            return asyncio.run(_is_vetted_tg_source(username, "SEAL-1", "software-house"))

    def test_a_vetted_handle_is_authorized(self):
        self.assertTrue(self._chiedi("therealdadabit", {"vetted": True}))

    def test_the_question_names_the_uri_the_direction_and_the_scope_of_the_binding(self):
        """Lo scope è quello della chat legata, non del chiamante: vagliare
        contro le fonti di un altro topic sbaglierebbe in permissivo (#364)."""
        self._chiedi("@TheRealDadabit", {"vetted": True})
        path, params = self.chiamate[0]
        self.assertEqual(path, "/internal/egress")
        self.assertEqual(params, {"uri": "tg:@TheRealDadabit", "direction": "ingress",
                                  "scope": "SEAL-1/software-house"})

    def test_an_unvetted_handle_is_refused(self):
        self.assertFalse(self._chiedi("estraneo", {"vetted": False}))

    def test_a_sender_without_a_handle_is_refused_without_even_asking(self):
        """`tg:` in ingresso registra un `@handle`, non un uid: un mittente senza
        handle non è vagliabile per costruzione, e va rifiutato senza esplodere."""
        self.assertFalse(self._chiedi(None, {"vetted": True}))
        self.assertFalse(self._chiedi("", {"vetted": True}))
        self.assertEqual(self.chiamate, [])

    def test_a_broken_gateway_authorizes_nobody(self):
        """Fail-closed: un guasto non è un permesso. È la direzione d'errore che
        non si vede, perché nessuno rilegge le autorizzazioni concesse."""
        self.assertFalse(self._chiedi("therealdadabit", boom=RuntimeError("503")))

    def test_a_reply_without_the_verdict_is_a_refusal(self):
        self.assertFalse(self._chiedi("therealdadabit", {"mode": "gate"}))


class RelayGateTests(unittest.TestCase):
    """Il percorso vero: chi è vagliato innesca il turno, chi non lo è riceve il
    rifiuto su Telegram e il topic non viene toccato."""

    BINDING = {"instance": "messaggero-1", "tier": "SEAL-1", "topic": "software-house"}

    def _run(self, vetted, testo="@clodia riassumi", username="therealdadabit"):
        import tempfile
        from pathlib import Path
        inviati, postati, turni = [], [], []

        async def _send(chat_id, text):
            inviati.append(text)
            return {}

        async def _open(tier, name):
            return {"meta": {"participants": ["clodia"]}}

        async def _post(tier, name, autore, testo, kind=None):
            postati.append(testo)

        async def _turn(tier, name, meta, trigger_text=""):
            turni.append(trigger_text)

        async def _vetted(u, tier, topic):
            return vetted

        d = Path(tempfile.mkdtemp())
        msg = {"message_id": 1, "from_id": 76632169, "from_username": username,
               "from": username, "text": testo}
        with patch.object(channel_relay, "_state_path", lambda c: d / "s.json"), \
                patch.object(channel_relay, "_is_vetted_tg_source", _vetted), \
                patch.object(channel_relay.telegram_client, "send_async", _send), \
                patch.object(channel_relay.topics_client, "async_open_topic", _open), \
                patch.object(channel_relay.topics_client, "async_post_message", _post), \
                patch.object(channel_relay, "run_topic_turn", _turn):
            asyncio.run(channel_relay._relay_chat("-5279916551", self.BINDING, [msg]))
        return inviati, postati, turni

    def test_a_vetted_sender_triggers_the_turn(self):
        inviati, postati, turni = self._run(True)
        self.assertTrue(any("Ricevuto" in t for t in inviati))
        self.assertEqual(len(postati), 1)
        self.assertIn("@clodia riassumi", postati[0])
        self.assertEqual(turni, ["@clodia riassumi"])

    def test_an_unvetted_sender_is_refused_and_the_topic_is_not_touched(self):
        inviati, postati, turni = self._run(False)
        self.assertEqual(inviati, [channel_relay._DENY])
        self.assertEqual(postati, [])
        self.assertEqual(turni, [])

    def test_chatter_that_does_not_address_the_bot_stays_in_the_buffer(self):
        """Nessuna regressione sulla precondizione: senza menzione non si
        autorizza e non si rifiuta nulla — è contesto, non una richiesta."""
        inviati, postati, turni = self._run(True, testo="guardate il doc")
        self.assertEqual((inviati, postati, turni), ([], [], []))


class AddressesBotTests(unittest.TestCase):
    def test_mention_bot(self):
        self.assertTrue(_addresses_bot("ehi @clodia_r_olivay_bot aiutami", []))
        self.assertTrue(_addresses_bot("@Clodia rispondi", []))

    def test_mention_agent_participant(self):

        """router-notebook R5: il relay verso il gruppo resta (decisione ribaltata il 12 ago)."""
        self.assertTrue(_addresses_bot("@ophelia che ne pensi?", ["ophelia", "davide"]))

    def test_human_chatter_not_addressed(self):
        self.assertFalse(_addresses_bot(
            "ciao @therealdadabit @matlemad ho aggiunto il doc", ["ophelia", "davide"]))


class NoWhitelistLeftTests(unittest.TestCase):
    """Il blocco `<!-- telegram-whitelist -->` non ha più un lettore.

    Un residuo che resta in giro è peggio del meccanismo che sostituiva: qualcuno
    lo aggiorna credendo di autorizzare qualcuno, e non succede niente.
    """

    def test_the_module_has_no_whitelist_reader(self):
        import inspect
        codice = [r for r in inspect.getsource(channel_relay).splitlines()
                  if "telegram-whitelist" in r or "telegram_whitelist" in r
                  or "_load_whitelist" in r or '"dialogue"' in r]
        self.assertEqual(codice, [], f"residui del meccanismo vecchio: {codice}")


class MessengerTests(unittest.TestCase):
    def test_matches(self):
        self.assertTrue(_is_messenger("messaggero-1"))
        self.assertFalse(_is_messenger("clodia"))


class ContextTests(unittest.TestCase):
    def _m(self, uid, uname, text):
        return {"from_id": uid, "from_username": uname, "from": uname, "text": text}

    def test_line_uses_group_name(self):
        m = self._m(76632169, "therealdadabit", "ciao")
        m["chat_title"] = "Proof-of-flex"
        line = _line(m, "-5279916551")
        self.assertEqual(line, "[tg://Proof-of-flex/therealdadabit] -> ciao")

    def test_line_falls_back_to_chat_id(self):
        line = _line({"from_id": 999, "text": "spam"}, "-5")
        self.assertEqual(line, "[tg://-5/999] -> spam")

    def test_line_with_saved_file(self):
        m = self._m(76632169, "therealdadabit", "")
        m["file"] = {"file_name": "report.pdf"}
        m["saved_file"] = "files/report.pdf"
        line = _line(m, "-5")
        self.assertIn("📎 report.pdf", line)
        self.assertIn("files/report.pdf", line)

    def test_line_file_download_failed(self):
        m = self._m(1, "u", "")
        m["file"] = {"file_name": "x.bin"}
        m["saved_file"] = ""
        self.assertIn("download non riuscito", _line(m, "-5"))

    def test_context_block_one_line_per_message(self):
        buffer = [self._m(107393046, "giocasu75", "guardate il doc"),
                  self._m(76632169, "therealdadabit", "@clodia riassumi")]
        block = _context_block(buffer, "-5279916551")
        self.assertEqual(block,
                         "[tg://-5279916551/giocasu75] -> guardate il doc\n"
                         "[tg://-5279916551/therealdadabit] -> @clodia riassumi")


if __name__ == "__main__":
    unittest.main()
