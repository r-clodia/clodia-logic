"""#704 — the agent-server log carries no message text.

`logs.tail` serves this log to sysadmin, whose charter is «never the
contents», and the file sits on the shared datadir. Until #704 every turn
wrote `turno START <chat>: <first 80 characters of the prompt>`, and a
narrative @mention logged the reply's sentence. The gateway redacts those
shapes on read (clodia-tools `logs.redact_content`); this test keeps new ones
from being written: no logger call may pass a prompt, a reply or a snippet of
one as an argument. A length (`len(...)`) is fine.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
LOGGERS = {"LOG", "log", "logger", "LOGGER", "_log"}
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
#: Helpers whose result IS message text.
TEXT_HELPERS = {"_snippet", "_reply_text", "_mention_context", "trace_io"}
#: Names that hold message text in this code base — as a bare name, an
#: attribute (`m.text`, `msg.body`) or a subscript key (`m["text"]`).
TEXT_NAMES = {"content", "reply", "reply_text", "prompt", "testo", "risposta",
              "text", "body", "message", "msg_text", "snippet", "preview"}
#: Reviewed exceptions: (file relative to server/, format-string prefix) — the
#: argument is not message text.
ALLOWED = {
    # The `claude auth login` CLI's own stdout (the OAuth URL), not a message.
    ("api/auth.py", "[claude auth login]"),
    # The stderr of the codex sandbox probe (`/bin/true`), not a message.
    ("sdk_runtime/session.py", "il sandbox di codex NON parte"),
    # An HTTP error body of the gateway's PDP (a JSON error), capped.
    ("api/gateway_pdp.py", "authorize '%s' per '%s'"),
}


def _is_length(a: ast.AST) -> bool:
    """The WHOLE argument is `len(...)`: a number, not the text. A `len(`
    buried in a larger expression exempts nothing (`x or len(y)`)."""
    return (isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
            and a.func.id == "len")


def _names_text(n: ast.AST) -> bool:
    if isinstance(n, ast.Name):
        return n.id in TEXT_NAMES
    if isinstance(n, ast.Attribute):
        return n.attr in TEXT_NAMES
    if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant):
        return n.slice.value in TEXT_NAMES
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
            and n.func.attr == "get" and n.args \
            and isinstance(n.args[0], ast.Constant):
        return n.args[0].value in TEXT_NAMES
    return False


def _offending(call: ast.Call) -> list[str]:
    bad = []
    for a in call.args[1:]:
        if _is_length(a):
            continue
        for n in ast.walk(a):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in TEXT_HELPERS:
                bad.append(ast.unparse(a))
                break
            if _names_text(n):
                bad.append(ast.unparse(a))
                break
    return bad


class NoMessageTextInLogsTests(unittest.TestCase):
    def test_no_logger_call_passes_message_text(self):
        found = []
        for path in sorted(ROOT.rglob("*.py")):
            if path.name.startswith("test_") or "/test_" in str(path):
                continue
            rel = str(path.relative_to(ROOT))
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in LEVELS and isinstance(n.func.value, ast.Name)
                        and n.func.value.id in LOGGERS and n.args):
                    continue
                fmt = n.args[0].value if isinstance(n.args[0], ast.Constant) else ""
                if any(rel == f and str(fmt).startswith(p) for f, p in ALLOWED):
                    continue
                for arg in _offending(n):
                    found.append(f"{rel}:{n.lineno}: {arg}")
        self.assertEqual(found, [], "message text in a log line (clodia-platform#704)")

    def test_the_guard_catches_the_shapes_it_claims(self):
        def calls(src):
            return [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "info"]
        caught = ['LOG.info("x %s", text)', 'LOG.info("x %s", m.text)',
                  'LOG.info("x %s", m["body"])', 'LOG.info("x %s", m.get("message"))',
                  'LOG.info("x %s", _snippet(content, 80))',
                  'LOG.info("x %s", text or len(text))',
                  'LOG.info("x %s", str(reply)[:80])']
        passed = ['LOG.info("x %d", len(content))', 'LOG.info("x %s", chat_id)',
                  'LOG.info("x %s", type(e).__name__)']
        for src in caught:
            self.assertTrue(_offending(calls(src)[0]), src)
        for src in passed:
            self.assertFalse(_offending(calls(src)[0]), src)

    def test_the_turn_start_line_logs_a_length(self):
        src = (ROOT / "sdk_runtime" / "session.py").read_text(encoding="utf-8")
        self.assertIn('LOG.info("turno START %s (%d chars)", self.chat_id, len(content or ""))',
                      src)
        self.assertNotIn('"turno START %s: %s"', src)


if __name__ == "__main__":
    unittest.main()
