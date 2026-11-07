"""Test support (clodia-platform#656): the gateway's GLOBAL tuples, faked.

Since #626 an agent's own verbs are the gateway's global A·V·R·Q tuples, asked
through `authz_grants.global_grants`; #656 removed the seed's `tool_permissions`
and the fallbacks that read it. The trifecta, RAG-membership and routing suites
were written as "an agent with these verbs": they now say it to this fake, which
plays the gateway for the duration of a test module.

Usage in a test module::

    from . import fake_global_grants as FG
    setUpModule, tearDownModule = FG.module_fixture()
    FG.give("clodia", ["email.send"])

Names not given are answered with `[]` (the gateway answers, the agent has no
global tuple) — not `None`, which means "gateway mute".

Not a test module (no `test_` prefix): imported by the tests only.
"""
from __future__ import annotations

from typing import Iterable, Optional
from unittest.mock import patch

_GRANTS: dict[str, list[str]] = {}


def give(name: str, verbs: Iterable[str]) -> None:
    """`name` holds these global verbs (replaces what it held)."""
    _GRANTS[str(name)] = [str(v) for v in (verbs or [])]


def clear() -> None:
    _GRANTS.clear()


def _fake(name: str) -> Optional[list[str]]:
    return list(_GRANTS.get(str(name or ""), []))


def module_fixture():
    """`(setUpModule, tearDownModule)` that patch `global_grants` for a module."""
    state: dict = {}

    def set_up():
        from . import authz_grants
        clear()
        authz_grants.clear()
        state["p"] = patch.object(authz_grants, "global_grants", _fake)
        state["p"].start()

    def tear_down():
        p = state.pop("p", None)
        if p is not None:
            p.stop()
        clear()

    return set_up, tear_down
