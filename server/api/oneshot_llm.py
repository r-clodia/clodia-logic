"""One model call, no tools: text in, text out (clodia-platform#708).

The platform's model calls go through an agent runtime (Claude SDK, opencode,
codex): a turn, with tools and a session. Some platform work needs neither —
the mail courier's summary of a delivered mail is a plain completion whose
worst outcome is a wrong summary, because a call without tools cannot act.

This module makes that call on the provider layer the runtimes use
(`providers`): the same catalogue, the same credentials (read from the vault
as `provider_env` reads them, never a key in code or in a log), the same
SEAL rule — a provider is eligible only if connected, not paused and its SEAL
is at least the room's tier (`provider_meets_tier`). Among the eligible the
cheapest wins (`cost_rank`), as for a spawn.

Only API-key providers are callable here: subscriptions (Claude Pro/Max/Team,
ChatGPT) authenticate a runtime, not a raw HTTP call. Supported wire formats:
OpenAI-compatible chat completions (openai-api, scaleway, generic-openai),
Anthropic Messages (anthropic-api), Bedrock Converse with an API key
(aws-region-eu).

The model is a small one per provider (`MODELS`), overridable with
`CLODIA_ONESHOT_MODELS` (JSON `{provider: model}`).
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from . import providers as P

LOG = logging.getLogger("agent-server.oneshot_llm")

#: A small, cheap model per callable provider.
MODELS: dict[str, str] = {
    "scaleway": "gemma-4-26b-a4b-it",
    "openai-api": "gpt-5-mini",
    "generic-openai": "",            # its configured `model`
    "anthropic-api": "claude-haiku-4-5",
    "aws-region-eu": "",             # the provider's Haiku profile (extra_env)
}

#: Wire format per provider.
_OPENAI = {"scaleway", "openai-api", "generic-openai"}
_ANTHROPIC = {"anthropic-api"}
_BEDROCK = {"aws-region-eu"}

DEFAULT_TIMEOUT_S = 20.0


class NoEligibleProvider(RuntimeError):
    """No connected API-key provider reaches the room's SEAL tier."""


def _models() -> dict[str, str]:
    out = dict(MODELS)
    raw = os.environ.get("CLODIA_ONESHOT_MODELS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                out.update({str(k): str(v) for k, v in extra.items() if v})
        except ValueError:
            LOG.warning("CLODIA_ONESHOT_MODELS non è JSON: ignorato")
    return out


def callable_providers() -> set[str]:
    return set(_OPENAI | _ANTHROPIC | _BEDROCK)


def pick_provider(tier: str | None) -> str:
    """The cheapest callable provider eligible at `tier`, or NoEligibleProvider."""
    connected = P.connected_provider_ids()
    paused = set(P.list_paused_providers())
    eligible = [p for p in callable_providers()
                if p in connected and p not in paused and P.provider_meets_tier(p, tier)]
    if not eligible:
        raise NoEligibleProvider(f"nessun provider a chiave API collegato regge {tier}")
    return min(eligible, key=lambda p: (P.provider_cost_rank(p), p))


def _model_for(pid: str, bundle: dict) -> str:
    m = _models().get(pid, "")
    if pid == "generic-openai":
        return m or str(bundle.get("model") or "")
    if pid == "aws-region-eu":
        return m or str(P.provider_extra_env(pid).get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or "")
    return m


def complete(tier: str | None, system: str, text: str, *, max_tokens: int = 200,
             timeout: float = DEFAULT_TIMEOUT_S) -> tuple[str, str]:
    """`(answer, provider)` of ONE tool-less completion at `tier`. Raises on
    no eligible provider, HTTP error or timeout: the caller decides what a
    missing answer means. Blocking: call it in a thread."""
    pid = pick_provider(tier)
    bundle = P._read(pid) or {}
    key = str(bundle.get("api_key") or "")
    model = _model_for(pid, bundle)
    if not model:
        raise NoEligibleProvider(f"nessun modello configurato per {pid}")
    with httpx.Client(timeout=timeout) as c:
        if pid in _OPENAI:
            base = (P.provider_base_url(pid, bundle) or "https://api.openai.com/v1").rstrip("/")
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"} if key else {},
                       json={"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": text}]})
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"] or ""
        elif pid in _ANTHROPIC:
            r = c.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                       json={"model": model, "max_tokens": max_tokens, "system": system,
                             "messages": [{"role": "user", "content": text}]})
            r.raise_for_status()
            out = "".join(b.get("text", "") for b in r.json().get("content") or []
                          if b.get("type") == "text")
        else:
            region = P.provider_extra_env(pid).get("AWS_REGION") or "eu-west-1"
            r = c.post(f"https://bedrock-runtime.{region}.amazonaws.com/model/{model}/converse",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"system": [{"text": system}],
                             "messages": [{"role": "user", "content": [{"text": text}]}],
                             "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2}})
            r.raise_for_status()
            out = "".join(b.get("text", "") for b in
                          ((r.json().get("output") or {}).get("message") or {}).get("content") or [])
    return str(out).strip(), pid
