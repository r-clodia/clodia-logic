"""The environment a spawn's runtime process starts with — the same rules for
claude, codex and opencode (clodia-platform#471, #463).

## Orchestrator-only secrets never reach a spawn (#471)

`CLODIA_ORCHESTRATOR_SECRET` is the bootstrap key of the gateway's minting:
whoever holds it has the gateway mint any identity. `GIT_TOKEN` is the
platform's PAT. The claude runtime dropped both since M3++; codex and opencode
built their env from `os.environ` and inherited them. One function now builds
it for all three, so a fourth runtime cannot forget.

## The egress label (#463)

The egress proxy sees `host:port` of a CONNECT and nothing inside it, so the
proxy record of a request can be joined to its turn only through what the
CONNECT itself carries: the proxy credentials. Every proxy variable the spawn
inherits becomes `http://<spawn>:<tag>@egress-proxy:8888`, with

    tag = HMAC-SHA256(orchestrator secret, "clodia-egress-attribution/v1|" + spawn)[:32]

— the same function as `audit.trace.egress_tag` in clodia-tools, which checks
it when the proxy reports the request. The spawn holds its own tag and not the
secret, so it cannot produce another spawn's. The tag is a label, not a
credential that opens anything: the proxy serves an unlabelled request too.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Mapping
from urllib.parse import quote, urlsplit, urlunsplit

#: Secrets of the orchestrator, never of a spawn.
ORCHESTRATOR_ONLY = ("CLODIA_ORCHESTRATOR_SECRET", "GIT_TOKEN")
#: The variables cooperative clients read for their proxy, both spellings.
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
              "https_proxy", "http_proxy", "all_proxy")

_SPAWN_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_EGRESS_DOMAIN = b"clodia-egress-attribution/v1|"


def egress_tag(spawn: str | None, secret: str | None = None) -> str | None:
    """The spawn's proxy credential, or None (no secret, or not a spawn label)."""
    key = (secret if secret is not None
           else os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    if not key or not spawn or not _SPAWN_LABEL.match(spawn):
        return None
    mac = hmac.new(key.encode("utf-8"), _EGRESS_DOMAIN + spawn.encode("utf-8"),
                   hashlib.sha256)
    return mac.hexdigest()[:32]


def label_proxy_url(url: str, spawn: str, tag: str) -> str:
    """`url` with `spawn:tag@` as its userinfo. A URL that already carries
    credentials is the operator's: left alone."""
    try:
        u = urlsplit(url)
    except ValueError:
        return url
    if not u.scheme or not u.hostname or u.username is not None:
        return url
    netloc = f"{quote(spawn, safe='')}:{tag}@{u.netloc}"
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def spawn_env(base: Mapping[str, str], spawn: str | None) -> dict[str, str]:
    """A copy of `base` fit for a spawn: without orchestrator-only secrets and
    the agent-server's own Langfuse keys (#471),
    without the runtimes' OTel content-capture switches (`otel_export`), and
    with its proxy variables labelled with the spawn (when there is a proxy, a
    spawn and a secret to sign with)."""
    from ..otel_export import strip_content_flags
    env = strip_content_flags({k: v for k, v in base.items()
                               if k not in ORCHESTRATOR_ONLY and not k.startswith("LANGFUSE_")})
    tag = egress_tag(spawn)
    if tag:
        for var in PROXY_VARS:
            if env.get(var):
                env[var] = label_proxy_url(env[var], spawn, tag)
    return env
