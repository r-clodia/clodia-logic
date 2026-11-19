"""A spawn's MCP token never reaches a command line (clodia-platform#706).

The argv of every process in the agent-server container is readable by every
other process (`/proc/<pid>/cmdline`, `ps auxww`). The Claude SDK used to put
the MCP config — `Authorization: Bearer ckt1…` included — on the CLI's argv
(`--mcp-config '<json>'`), and opencode started `mcp-remote --header
"Authorization: Bearer ckt1…"`: any spawn with a shell could read every other
live spawn's token and call the gateway as that agent.

These tests build the real command lines — the SDK's own `_build_command` for
Claude, the `command` opencode will exec for the bridge — and assert the token
is not in them; that the file holding it is 0600 and owned by the spawn uid;
and that mcp-remote is handed a placeholder its own environment resolves.
"""
from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from . import private_config
from . import session as S

TOKEN = "ckt1.eyJhZ2VudCI6ImNsb2RpYSJ9.SIGNATURE-THAT-MUST-NOT-LEAK"


def _claude_argv(opts_kwargs: dict) -> list[str]:
    """The argv the SDK would exec for these options (no process started)."""
    kw = {k: v for k, v in opts_kwargs.items() if k != "stderr"}
    kw.setdefault("cli_path", "/usr/bin/true")
    transport = SubprocessCLITransport(prompt="x", options=ClaudeAgentOptions(**kw))
    return transport._build_command()


def _start_claude(spawn_dir: Path, *, sandbox_uid=None) -> S.ChatSession:
    """Run `ChatSession.start()` up to the client, with the SDK client mocked."""
    with mock.patch.object(S, "known_kind", return_value=True):
        sess = S.ChatSession("chan:SEAL-1:t:clodia", kind="clodia")
    captured = {}

    async def _fake_open(self):
        captured["opts"] = dict(self._opts_kwargs)

    patches = [
        mock.patch.object(S, "_materialize_spawn", return_value=(None, spawn_dir)),
        mock.patch.object(S.pki, "mint_session_token", return_value=TOKEN),
        mock.patch.object(S.ChatSession, "_open_client", _fake_open),
        mock.patch.object(S, "_sandbox_enabled", return_value=sandbox_uid is not None),
        mock.patch.object(S, "_resolve_disallowed_tools", return_value=[]),
        mock.patch.object(S, "_resolve_native_denied", return_value=[]),
        mock.patch.object(S, "_resolve_auto_approved", return_value=[]),
        mock.patch.object(S, "_resolve_permission_mode", return_value=None),
        mock.patch.object(S, "_runtime_model", return_value=None),
        mock.patch.object(S, "_effective_clearance", return_value="SEAL-1"),
        mock.patch.dict(S.KIND_AUTO_INTRO, {}, clear=True),
    ]
    if sandbox_uid is not None:
        patches.append(mock.patch.object(
            S, "_sandbox_prepare",
            return_value=(sandbox_uid, ["/clodia/docker/agent-sandbox-exec.sh"],
                          {"CLODIA_AGENT_UID": str(sandbox_uid)})))
    for p in patches:
        p.start()
    try:
        asyncio.run(sess.start())
    finally:
        for p in reversed(patches):
            p.stop()
    sess._captured_opts = captured["opts"]
    return sess


class ClaudeTokenOffArgvTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.spawn = Path(self._td.name) / "clodia-1"
        self.spawn.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_the_cli_argv_carries_a_path_not_the_token(self):
        sess = _start_claude(self.spawn)
        opts = sess._captured_opts
        self.assertIsInstance(opts["mcp_servers"], str)
        argv = _claude_argv(opts)
        joined = "\0".join(argv)
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn("Bearer", joined)
        i = argv.index("--mcp-config")
        self.assertEqual(argv[i + 1], str(self.spawn / private_config.MCP_CONFIG_NAME))

    def test_the_token_is_in_no_environment_variable_of_the_cli(self):
        sess = _start_claude(self.spawn)
        env = sess._captured_opts.get("env") or {}
        self.assertFalse([k for k, v in env.items() if TOKEN in str(v)])

    def test_the_config_file_is_0600_and_carries_the_server(self):
        sess = _start_claude(self.spawn)
        path = Path(sess._captured_opts["mcp_servers"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        cfg = json.loads(path.read_text())
        srv = cfg["mcpServers"]["clodia-tools"]
        self.assertEqual(srv["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(srv["type"], "http")

    def test_under_the_sandbox_the_file_is_owned_by_the_spawn_uid(self):
        chowned = []
        with mock.patch.object(private_config.os, "fchown",
                               side_effect=lambda fd, u, g: chowned.append((u, g))):
            sess = _start_claude(self.spawn, sandbox_uid=60123)
        self.assertEqual(chowned, [(60123, S._seed_gid("clodia"))])
        argv = _claude_argv(sess._captured_opts)
        self.assertNotIn(TOKEN, "\0".join(argv))

    def test_a_re_mint_rewrites_the_file_and_stays_off_argv(self):
        sess = _start_claude(self.spawn)
        sess.principal = "davide"
        with mock.patch.object(S.pki, "mint_session_token", return_value=TOKEN + "-2"), \
             mock.patch.object(S, "_effective_clearance", return_value="SEAL-1"):
            self.assertTrue(sess._refresh_mcp_principal())
        path = Path(sess._opts_kwargs["mcp_servers"])
        self.assertIn(TOKEN + "-2", path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, "\0".join(_claude_argv(sess._opts_kwargs)))

    def test_without_a_spawn_dir_the_file_goes_to_a_private_dir_removed_at_stop(self):
        with mock.patch.object(S, "known_kind", return_value=True):
            sess = S.ChatSession("chan:SEAL-1:t:ada", kind="clodia")
        sess._mcp_servers = {"clodia-tools": {"type": "http", "url": "http://x",
                                              "headers": {"Authorization": f"Bearer {TOKEN}"}}}
        path = sess._write_mcp_config(None)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        sess._drop_mcp_config()
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())


class PrivateFileTests(unittest.TestCase):
    def test_a_planted_symlink_is_replaced_not_followed(self):
        with tempfile.TemporaryDirectory() as td:
            victim = Path(td) / "victim"
            victim.write_text("untouched")
            link = Path(td) / private_config.MCP_CONFIG_NAME
            link.symlink_to(victim)
            private_config.write_mcp_config(Path(td), {"s": {}})
            self.assertEqual(victim.read_text(), "untouched")
            self.assertFalse(link.is_symlink())
            self.assertEqual(stat.S_IMODE(link.stat().st_mode), 0o600)


def _opencode_session(kind: str = "impiegato-tomato") -> S.OpenCodeChatSession:
    sess = S.OpenCodeChatSession.__new__(S.OpenCodeChatSession)
    sess.kind = kind
    sess.chat_id = f"chan:SEAL-2:test:{kind}"
    sess.principal = "davide"
    sess._runtime_override = {}
    sess._provider = None
    sess._model = None
    sess._spawn = None
    return sess


class OpenCodeTokenOffArgvTests(unittest.TestCase):
    def _write(self, td: Path) -> tuple[dict, dict]:
        spec = SimpleNamespace(reasoning_effort=None, sandbox=None)
        with mock.patch.object(S, "_runtime_provider", return_value="scaleway"), \
             mock.patch.object(S, "_runtime_model", return_value="glm-5.2"), \
             mock.patch.object(S, "_kind_spec", return_value=spec), \
             mock.patch("server.api.providers._read", return_value={}), \
             mock.patch("server.api.providers.provider_extra_env", return_value={}), \
             mock.patch.object(S.pki, "mint_session_token", return_value=TOKEN):
            env = _opencode_session()._write_config(td)
        return env, json.loads((td / "opencode.json").read_text())

    def test_the_bridge_argv_names_an_env_var_not_the_token(self):
        with tempfile.TemporaryDirectory() as td:
            _env, cfg = self._write(Path(td))
        mcp = cfg["mcp"]["clodia-tools"]
        argv = "\0".join(mcp["command"])
        self.assertNotIn(TOKEN, argv)
        self.assertNotIn("Bearer", argv)
        i = mcp["command"].index("--header")
        self.assertEqual(mcp["command"][i + 1],
                         "Authorization:${%s}" % S.OPENCODE_MCP_AUTH_ENV)
        self.assertEqual(mcp["environment"], {S.OPENCODE_MCP_AUTH_ENV: f"Bearer {TOKEN}"})

    def test_the_token_is_not_in_the_env_of_opencode_serve(self):
        with tempfile.TemporaryDirectory() as td:
            env, _cfg = self._write(Path(td))
        self.assertFalse([k for k, v in env.items() if TOKEN in str(v)])

    def test_the_config_file_is_0600(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(Path(td))
            mode = stat.S_IMODE((Path(td) / "opencode.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_the_placeholder_matches_what_mcp_remote_expands(self):
        """mcp-remote's `substituteEnvVars`: /\\$\\{([^}]+)}/ against its env."""
        import re
        header = "Authorization:${%s}" % S.OPENCODE_MCP_AUTH_ENV
        env = {S.OPENCODE_MCP_AUTH_ENV: f"Bearer {TOKEN}"}
        expanded = re.sub(r"\$\{([^}]+)}", lambda m: env[m.group(1)], header)
        self.assertEqual(expanded, f"Authorization:Bearer {TOKEN}")


class NoTokenOnArgvAnywhereTests(unittest.TestCase):
    """Static guard: no runtime builds a `Bearer` value into an argv list."""

    def test_session_module_has_no_bearer_in_a_command(self):
        src = Path(S.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"--header", f"Authorization', src)
        self.assertNotIn("--mcp-config", src.replace("`--mcp-config", ""))


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTests(unittest.TestCase):
    """Independent review of #614: S2, M1, M7."""

    def test_m7_mcp_remote_is_pinned(self):
        with tempfile.TemporaryDirectory() as td:
            _env, cfg = OpenCodeTokenOffArgvTests()._write(Path(td))
        self.assertIn(f"mcp-remote@{S.MCP_REMOTE_VERSION}", cfg["mcp"]["clodia-tools"]["command"])
        self.assertEqual(S.MCP_REMOTE_VERSION, "0.16.7")

    def test_s2_the_opencode_client_authenticates_and_the_password_is_off_argv(self):
        sess = _opencode_session()
        sess._oc_password = "pw-only-in-memory"
        client = sess._oc_http(base_url="http://127.0.0.1:1")
        try:
            import httpx
            req = client.build_request("GET", "/doc")
            req = next(client._auth.sync_auth_flow(req)) if client._auth else req
            self.assertEqual(req.headers["authorization"],
                             httpx.BasicAuth(S.OPENCODE_SERVER_USERNAME, "pw-only-in-memory")
                             ._auth_header)
        finally:
            asyncio.run(client.aclose())

    def test_s2_start_puts_the_password_in_the_serve_env_not_its_argv(self):
        sess = _opencode_session()
        sdir = Path(tempfile.mkdtemp())
        sess._proc = None
        sess._sandbox_uid = None
        captured = {}

        async def _fake_exec(*argv, env=None, **kw):
            captured["argv"], captured["env"] = argv, env
            raise RuntimeError("stop here")
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(S, "_materialize_spawn", return_value=(None, Path(td))), \
             mock.patch.object(S.OpenCodeChatSession, "_write_config", return_value={}), \
             mock.patch.object(S, "_sandbox_prepare", return_value=(None, [], {})), \
             mock.patch.object(S.asyncio, "create_subprocess_exec", _fake_exec), \
             mock.patch.object(S.OpenCodeChatSession, "sessions_dir",
                               new=property(lambda self: sdir)):
            with self.assertRaises(RuntimeError):
                asyncio.run(sess.start())
        pw = captured["env"]["OPENCODE_SERVER_PASSWORD"]
        self.assertTrue(len(pw) >= 32)
        self.assertEqual(pw, sess._oc_password)
        self.assertNotIn(pw, " ".join(map(str, captured["argv"])))

    def test_m1_the_system_prompt_goes_as_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            spawn = Path(td) / "clodia-1"
            spawn.mkdir()
            (spawn / "system-prompt.md").write_text("SECRET SYSTEM PROMPT")
            sess = _start_claude(spawn)
            argv = _claude_argv(sess._captured_opts)
        self.assertNotIn("SECRET SYSTEM PROMPT", "\0".join(argv))
        i = argv.index("--system-prompt-file")
        self.assertEqual(argv[i + 1], str(spawn / "system-prompt.md"))
