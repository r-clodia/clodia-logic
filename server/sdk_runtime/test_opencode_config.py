from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from . import session as S
from .session import OpenCodeChatSession


def _session(kind: str = "impiegato-tomato") -> OpenCodeChatSession:
    sess = OpenCodeChatSession.__new__(OpenCodeChatSession)
    sess.kind = kind
    sess.chat_id = f"chan:SEAL-2:test:{kind}"
    sess.principal = "davide"
    sess._runtime_override = {}
    sess._provider = None
    sess._model = None
    return sess


class OpenCodeConfigTests(unittest.TestCase):

    def test_seed_reasoning_effort_is_written_as_opencode_model_option(self):
        spec = SimpleNamespace(reasoning_effort="none")

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(S, "_runtime_provider", return_value="scaleway"), \
             mock.patch.object(S, "_runtime_model", return_value="glm-5.2"), \
             mock.patch.object(S, "_kind_spec", return_value=spec), \
             mock.patch("server.api.providers._read", return_value={"api_key": "sk-test"}), \
             mock.patch("server.api.providers.provider_extra_env",
                        return_value={"OPENAI_BASE_URL": "https://api.scaleway.ai/v1"}), \
             mock.patch.object(S.pki, "mint_session_token", return_value="ckt1.test"):
            env = _session()._write_config(Path(td))
            cfg = json.loads((Path(td) / "opencode.json").read_text(encoding="utf-8"))

        self.assertEqual(env["OPENCODE_PROVIDER_KEY"], "sk-test")
        self.assertEqual(
            cfg["provider"]["scaleway"]["models"]["glm-5.2"]["options"]["reasoningEffort"],
            "none",
        )
        self.assertEqual(
            cfg["provider"]["scaleway"]["options"]["baseURL"],
            "https://api.scaleway.ai/v1",
        )

    def test_glm_52_defaults_to_no_reasoning_for_stale_imported_seed(self):
        stale_spec = SimpleNamespace(reasoning_effort=None)

        with mock.patch.object(S, "_kind_spec", return_value=stale_spec):
            self.assertEqual(S._opencode_reasoning_effort("impiegato-tomato", "glm-5.2"), "none")

    def test_other_models_do_not_get_implicit_reasoning_override(self):
        stale_spec = SimpleNamespace(reasoning_effort=None)

        with mock.patch.object(S, "_kind_spec", return_value=stale_spec):
            self.assertIsNone(S._opencode_reasoning_effort("worker", "qwen3-coder"))


if __name__ == "__main__":
    unittest.main()
