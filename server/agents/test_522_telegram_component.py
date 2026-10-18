"""comms-pack 2.1.0 — Telegram as a GATEWAY component (clodia-platform#522/#558).

Run against a fake host (the component never imports the gateway): what it
registers, where the token and the state come from, and that identity and room
come from the call's claims, never from the caller."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

from ..config import workspace_path

GATEWAY = Path(workspace_path("catalogs/packs/comms-pack/gateway"))
PKG = "tgcomp_under_test"


def _load():
    for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
        sys.modules.pop(k)
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(GATEWAY)]
    sys.modules[PKG] = pkg
    spec = importlib.util.spec_from_file_location(f"{PKG}.telegram_component",
                                                  GATEWAY / "telegram_component.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True       # the pack ships no bytecode
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = before
    return mod


class FakeCreds:
    def __init__(self, bundle):
        self.bundle, self.reads = bundle, []

    def read_internal(self, name):
        self.reads.append(name)
        return self.bundle


class FakeHost:
    def __init__(self, state_dir):
        self.verbs, self.services = {}, {}
        self.creds = FakeCreds({"token": "123:ABC", "bot_username": "x"})
        self.asked = []
        self._dir = state_dir

    def credentials(self, prefix):
        self.asked.append(prefix)
        return self.creds

    def state_path(self, name):
        return str(Path(self._dir) / name)

    def register_verb(self, tool, handler):
        self.verbs[tool.name] = (tool, handler)

    def register_service(self, name, handler):
        self.services[name] = handler

    def agent(self):
        return "messaggero"

    def channel(self):
        return "chan:SEAL-1:t:messaggero"


class TelegramComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = _load()
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == PKG or k.startswith(PKG + ".")])
        self.host = FakeHost(tmp.name)
        self.mod.register(self.host)
        self.core = sys.modules[f"{PKG}.telegram_core"]

    def test_what_it_registers(self) -> None:
        self.assertEqual(set(self.host.verbs), {
            "telegram.inbox", "telegram.lease_acquire", "telegram.poll", "telegram.send",
            "telegram.send_file", "telegram.lease_release", "telegram.roster",
            "telegram.status"})
        self.assertEqual(set(self.host.services), {"telegram_updates", "telegram_download",
                                                   "telegram_status", "telegram_notify",
                                                   "telegram_chat_admins"})
        self.assertNotIn("telegram.updates", self.host.verbs)   # never an agent verb

    def test_token_from_the_vault_field_and_state_from_the_host(self) -> None:
        self.assertEqual(self.host.asked, ["telegram_bot_token"])
        self.assertEqual(self.core._token(), "123:ABC")
        self.assertEqual(self.host.creds.reads, ["telegram_bot_token"])
        self.assertTrue(str(self.core._state_path()).endswith("telegram-state.json"))
        self.assertTrue(str(self.core._state_path()).startswith(self.host._dir))

    def test_identity_and_room_come_from_the_claims(self) -> None:
        seen = {}
        self.core.DISPATCH["lease_release"] = lambda a: seen.update(a) or {}
        # the registered handler closed over the original function: rebuild it
        handler = self.mod._handler(self.host, self.core.DISPATCH["lease_release"])
        handler({"chat_id": "1", "agent": "forged", "channel": "chan:SEAL-4:x:y"})
        self.assertEqual(seen["agent"], "messaggero")
        self.assertEqual(seen["channel"], "chan:SEAL-1:t:messaggero")

    def test_no_token_reads_as_not_configured(self) -> None:
        self.host.creds.bundle = {}
        self.assertFalse(self.core.status({})["configured"])
        with self.assertRaises(RuntimeError):
            self.core._token()


class GatewayServicesTests(unittest.TestCase):
    """The gateway's own uses of the bot (comms-pack 2.2.0, #522)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = _load()
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == PKG or k.startswith(PKG + ".")])
        self.host = FakeHost(tmp.name)
        self.mod.register(self.host)
        self.core = sys.modules[f"{PKG}.telegram_core"]
        self.calls = []
        self.core.api_call = lambda token, method, params=None, timeout=15: (
            self.calls.append((token, method, params)) or {"message_id": 7})

    def test_a_token_only_credential_gets_its_username_from_getme(self) -> None:
        """The generic connect route stores `{token}` only: the component asks
        Telegram once and remembers (#522 review)."""
        self.host.creds.bundle = {"token": "123:ABC"}
        self.core.api_call = lambda token, method, params=None, timeout=15: (
            self.calls.append((token, method)) or {"username": "clodia_bot"})
        self.assertEqual(self.host.services["telegram_status"]()["bot_username"], "clodia_bot")
        self.assertEqual(self.core.BOT_USERNAME_SOURCE(), "clodia_bot")
        self.assertEqual(self.calls, [("123:ABC", "getMe")])          # asked once

    def test_status_never_carries_the_token(self) -> None:
        st = self.host.services["telegram_status"]()
        self.assertTrue(st["configured"])
        self.assertNotIn("123:ABC", repr(st))

    def test_notify_sends_to_the_numeric_chat_the_gateway_names(self) -> None:
        out = self.host.services["telegram_notify"]({"chat_id": "76632169", "text": "gate"})
        self.assertEqual(out, {"ok": True, "message_id": 7})
        self.assertEqual(self.calls, [("123:ABC", "sendMessage",
                                       {"chat_id": 76632169, "text": "gate"})])

    def test_chat_admins_of_a_numeric_chat(self) -> None:
        self.core.api_call = lambda token, method, params=None, timeout=15: (
            self.calls.append((method, params)) or [{"user": {"id": 1}}])
        self.assertEqual(self.host.services["telegram_chat_admins"]({"chat_id": "-100"}),
                         [{"user": {"id": 1}}])
        self.assertEqual(self.calls, [("getChatAdministrators", {"chat_id": -100})])
        with self.assertRaises(ValueError):
            self.host.services["telegram_chat_admins"]({"chat_id": "@group"})

    def test_notify_refuses_names_and_empty_text(self) -> None:
        for args in ({"chat_id": "Clodia Sviluppo", "text": "x"}, {"chat_id": "", "text": "x"},
                     {"chat_id": "1", "text": "  "}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.host.services["telegram_notify"](args)
        self.assertEqual(self.calls, [])


class ForumThreadTests(unittest.TestCase):
    """clodia-platform#548 on the component (moved from clodia-tools'
    native-client tests, #522): the forum thread reaches Telegram, its absence
    changes nothing, a meaningless value is refused before any call, and
    inbound messages remember their thread."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = _load()
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules)
                                 if k == PKG or k.startswith(PKG + ".")])
        self.host = FakeHost(tmp.name)
        self.mod.register(self.host)
        self.core = sys.modules[f"{PKG}.telegram_core"]
        self.calls = []
        self.core.api_call = lambda token, method, params=None, timeout=15: (
            self.calls.append((method, dict(params or {}))) or {"message_id": 7})
        self.core._post_multipart = lambda token, method, fields, *a, **k: (
            self.calls.append((method, dict(fields))) or {"message_id": 8})

    def test_send_forwards_the_thread(self) -> None:
        self.core.send({"chat_id": "-1004433615610", "text": "ciao", "thread_id": 42})
        self.assertEqual(self.calls[-1][1]["message_thread_id"], 42)

    def test_without_thread_the_payload_is_unchanged(self) -> None:
        self.core.send({"chat_id": "-1004433615610", "text": "ciao"})
        self.assertNotIn("message_thread_id", self.calls[-1][1])

    def test_send_file_forwards_the_thread(self) -> None:
        import base64
        self.core.send_file({"chat_id": "-1", "filename": "a.txt", "thread_id": "9",
                             "content_b64": base64.b64encode(b"x").decode()})
        self.assertEqual(self.calls[-1][1]["message_thread_id"], 9)

    def test_a_meaningless_thread_is_refused_before_calling_telegram(self) -> None:
        for bad in (0, -3, "abc", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.core.send({"chat_id": "-1", "text": "x", "thread_id": bad})
        self.assertEqual(self.calls, [])

    def test_thread_id_of_accepts_only_positive_ints(self) -> None:
        self.assertIsNone(self.core.thread_id_of(None))
        self.assertIsNone(self.core.thread_id_of(""))
        self.assertEqual(self.core.thread_id_of("12"), 12)

    def test_inbound_messages_keep_their_forum_thread(self) -> None:
        upd = {"update_id": 1}
        msg = {"message_id": 3, "text": "ciao", "is_topic_message": True,
               "message_thread_id": 77, "chat": {"id": -1, "type": "supergroup",
                                                 "title": "g"},
               "from": {"id": 5, "username": "u"}, "date": 0}
        self.assertEqual(self.core._message_row(upd, msg).get("thread_id"), 77)


class CommsComponentTests(unittest.TestCase):
    """The ONE gateway component registers each half only where its switch is
    on `pack` (`host.serves`)."""

    def _register(self, served):
        for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
            sys.modules.pop(k)
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(GATEWAY)]
        sys.modules[PKG] = pkg
        spec = importlib.util.spec_from_file_location(f"{PKG}.comms_component",
                                                      GATEWAY / "comms_component.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        before = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(mod)
            calls = []
            fake_mail = types.SimpleNamespace(register=lambda h: calls.append("mail"))
            fake_tg = types.SimpleNamespace(register=lambda h: calls.append("telegram"))
            sys.modules[f"{PKG}.mail_component"] = fake_mail
            sys.modules[f"{PKG}.telegram_component"] = fake_tg
            pkg.mail_component = fake_mail
            pkg.telegram_component = fake_tg
            host = types.SimpleNamespace(serves=lambda ns: ns in served)
            mod.register(host)
            return calls
        finally:
            sys.dont_write_bytecode = before
            for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
                sys.modules.pop(k)

    def test_each_half_where_its_switch_is_on_pack(self) -> None:
        self.assertEqual(self._register({"email", "telegram"}), ["mail", "telegram"])
        self.assertEqual(self._register({"telegram"}), ["telegram"])
        self.assertEqual(self._register({"email"}), ["mail"])
        self.assertEqual(self._register(set()), [])


class EveryVerbIsClassifiedTests(unittest.TestCase):
    """The gateway serves a COMPONENT verb only when the manifest classifies
    it (egress/source mapping, taint, or `unmapped`): an unclassified one is
    denied (review of clodia-logic#542 — five of eight were)."""

    def test_each_agent_verb_is_classified_and_reading_ones_taint(self) -> None:
        import yaml
        doc = yaml.safe_load((GATEWAY.parent / "pack.yaml").read_text(encoding="utf-8"))
        conn = next(c for c in doc["connectors"] if c["id"] == "telegram")
        mapped = {str(v.get("verb")) for v in conn.get("verbs") or [] if isinstance(v, dict)}
        taint = set(conn.get("taint") or [])
        unmapped = set(conn.get("unmapped") or [])
        mod = _load()
        try:
            agent = {f"telegram.{n}" for n in mod.AGENT_VERBS}
        finally:
            for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
                sys.modules.pop(k)
        self.assertEqual(agent - (mapped | taint | unmapped), set())
        # the verbs that return third-party text taint the channel
        self.assertLessEqual({"telegram.inbox", "telegram.poll", "telegram.roster"}, taint)


if __name__ == "__main__":
    unittest.main()
