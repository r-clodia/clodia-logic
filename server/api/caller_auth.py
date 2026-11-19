"""Every agent-server route has a signed caller (clodia-platform#705).

Before this module, identity on the agent-server was *additive* (F2a): a route
read the session token if there was one and carried on anonymously if not.
About sixty routes never looked at all, and some of them carried contents —
`GET /topics/{tier}/{name}/summary` fetched any topic, at any tier, with the
agent-server's own service token. Every spawn shares the container's network,
so `curl localhost:7842/topics/SEAL-3/<name>/summary` answered anyone.

The rule is now global and fail-closed. `require_signed_caller` is a dependency
of the whole application (see `main.create_app`): a request reaches a handler
only if it carries

* a session token (`Authorization: Bearer ckt1…`) that verifies against the
  colony CA — a person's WebUI session, an agent's own token, a token minted
  on someone's behalf; or
* the secret shared with the paired gateway (`X-Orchestrator-Secret`, the same
  credential the two services already use toward each other). It is never in
  a spawn's environment (`sdk_runtime.child_env.ORCHESTRATOR_ONLY`, #471).

The only exceptions are listed in `PUBLIC` and `BOOTSTRAP`, each with the
reason it may answer a stranger. `test_705_every_route_has_a_caller` walks the
application's routes and fails on any route that is neither guarded nor listed
here, so a new route cannot be added open by omission.

Authentication is the floor, not the decision: an agent's token is a signed
caller too. Routes that return contents decide on WHO the caller is — see
`caller_principal`, `caller_clearance`, `require_topic_reader` and
`is_paired_gateway`.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Callable, Optional

from fastapi import HTTPException, Request

LOG = logging.getLogger("agent-server.api.caller_auth")

#: Routes that answer without a signed caller, each with the reason. Keys are
#: `(METHOD, route template)`; a `HEAD` is judged as its `GET`.
PUBLIC: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness probe of the container; no data",
    ("GET", "/profile"):
        "edition features and branding for the login screen; no secret "
        "(instance_profile.public_view)",
    ("GET", "/profile/logo"):
        "the edition's logo on the login screen, loaded by an <img> that "
        "cannot carry a header; path confined to the datadir",
    ("GET", "/api/admin/state"):
        "bootstrap: whether the instance is claimed, read before any login",
    ("POST", "/clodia/whoami"):
        "login itself: verifies the signed token in its body against every "
        "human certificate",
    ("POST", "/clodia/cert-request"):
        "a person without a certificate asks the admin for one; it creates "
        "nothing, only notifies",
    ("GET", "/gate/{token}"):
        "signed gate link sent by mail: verifies its own token (gate_sign)",
    ("POST", "/gate/{token}/decide"):
        "same signed gate link: verifies its own token (gate_sign)",
    ("GET", "/api/agents/{name}/pfp"):
        "agent avatar image, loaded by an <img> that cannot carry a header",
    ("GET", "/topics/{tier}/{name}/download"):
        "signed file link (exp/sig from /download-url, which checks login and "
        "membership) opened by the webui with window.open, which carries no "
        "header; the handler verifies the signature and otherwise requires a "
        "member's session (anonymous → 401)",
    ("POST", "/topics/{classification}/{name}/refresh"):
        "event trigger of the topic-management helper (`topic end`, a bare "
        "curl): fast-forwards a legacy topic's working copy to its own origin; "
        "an anonymous caller gets back only the pull status, no content",
    # FastAPI's own schema and docs: Starlette routes outside the dependency
    # system. They describe the API, they hold no data.
    ("GET", "/openapi.json"): "API schema; no data",
    ("GET", "/docs"): "API docs page; no data",
    ("GET", "/docs/oauth2-redirect"): "API docs helper; no data",
    ("GET", "/redoc"): "API docs page; no data",
}

#: Routes that answer without a signed caller ONLY while the predicate holds.
BOOTSTRAP: dict[tuple[str, str], tuple[str, Callable[[], bool]]] = {}


def _not_initialized() -> bool:
    from . import admin
    return not admin.is_initialized()


BOOTSTRAP[("POST", "/api/agents")] = (
    "creating the first superadmin while the instance is unclaimed (the "
    "bootstrap gate in main.py refuses everything else then); afterwards a "
    "signed caller is required",
    _not_initialized,
)


def _route_key(request: Request) -> tuple[str, str]:
    method = request.method.upper()
    if method == "HEAD":
        method = "GET"
    route = request.scope.get("route")
    path = getattr(route, "path_format", None) or getattr(route, "path", None) \
        or request.url.path
    return method, path


def is_public(request: Request) -> bool:
    key = _route_key(request)
    if key in PUBLIC:
        return True
    cond = BOOTSTRAP.get(key)
    return bool(cond and cond[1]())


def is_paired_gateway(request: Request) -> bool:
    """True only with a secret configured AND presented: the caller is the
    gateway this agent-server is paired with. No secret configured → never."""
    expected = (os.environ.get("CLODIA_ORCHESTRATOR_SECRET") or "").strip()
    got = (request.headers.get("x-orchestrator-secret") or "").strip()
    return bool(expected and got and secret_matches(expected, got))


def secret_matches(expected: str, got: str) -> bool:
    """Constant-time comparison on BYTES: `hmac.compare_digest` on two `str`
    raises TypeError for non-ASCII input, i.e. a 500 where the answer is no."""
    return hmac.compare_digest(expected.encode("utf-8", "surrogateescape"),
                               got.encode("utf-8", "surrogateescape"))


_UNSET = object()


def caller_claims(request: Request) -> Optional[dict]:
    """The verified claims of the caller's session token, or None. Memoised on
    the request: the global guard and the handler verify once."""
    state = request.state
    cached = getattr(state, "_caller_claims", _UNSET)
    if cached is not _UNSET:
        return cached
    from .agents import _verified_claims
    claims = _verified_claims(request)
    state._caller_claims = claims
    return claims


def caller_principal(request: Request) -> Optional[str]:
    """WHO is acting by the signature: the person on an on-behalf token, the
    agent otherwise (same rule as `agents._signed_actor`)."""
    claims = caller_claims(request)
    if not claims:
        return None
    if claims.get("on_behalf"):
        person = str(claims.get("principal") or "").strip()
        if person:
            return person
    return str(claims.get("agent") or "").strip() or None


def caller_clearance(request: Request, principal: str) -> str:
    """The clearance the caller acts with: the signed `clearance` claim of an
    agent token (the spawn's effective clearance) capped by the principal's
    registered one; the registered one alone for a person's session."""
    from .channels import _CLEAR, _norm
    from .topics import _principal_clearance
    registered = _principal_clearance(principal)
    claims = caller_claims(request) or {}
    signed = claims.get("clearance")
    if signed and not claims.get("on_behalf"):
        if _CLEAR.get(_norm(signed), 0) < _CLEAR.get(_norm(registered), 0):
            return str(signed)
    return registered


def require_signed_caller(request: Request) -> None:
    """Global dependency of the agent-server (clodia-platform#705)."""
    if is_public(request):
        return
    if caller_claims(request) is not None or is_paired_gateway(request):
        return
    method, path = _route_key(request)
    LOG.warning("caller_auth: 401 %s %s (no signed caller)", method, path)
    raise HTTPException(401, "signed caller required: send the session token "
                             "(Authorization: Bearer ckt1…)")


def require_gateway_or_admin(request: Request) -> str:
    """The paired gateway, or a person who is an admin. Returns who."""
    if is_paired_gateway(request):
        return "gateway"
    from . import admin
    who = caller_principal(request)
    if who and admin.is_admin(who) and admin.is_human(who):
        return who
    raise HTTPException(403, "reserved to the paired gateway or an admin")


def require_gateway(request: Request) -> None:
    if not is_paired_gateway(request):
        raise HTTPException(403, "reserved to the paired gateway")


def can_read_topic(request: Request, tier: str, meta: dict) -> bool:
    """Both axes of a topic read: the caller is owner or participant of the
    room AND acts with a clearance at least the topic's tier."""
    from .channels import _can_access, _scope_role
    who = caller_principal(request)
    if not who or _scope_role(meta or {}, who) is None:
        return False
    return _can_access(caller_clearance(request, who), (meta or {}).get("tier") or tier)


def require_topic_reader(request: Request, tier: str, meta: dict) -> str:
    """`can_read_topic` or 403 — the same 403 whether the topic is above the
    caller's clearance or the caller is not in the room."""
    if not caller_claims(request):
        raise HTTPException(401, "login richiesto")
    if not can_read_topic(request, tier, meta):
        raise HTTPException(403, "non sei partecipante di questo canale "
                                 "(o il suo tier supera la tua clearance)")
    return caller_principal(request) or ""


class GatewayRequestForTests:
    """A stand-in `Request` that presents the paired-gateway secret, for unit
    tests that call a handler directly. Pair it with `CLODIA_ORCHESTRATOR_SECRET`
    set to `SECRET_FOR_TESTS` (e.g. `mock.patch.dict(os.environ, …)`)."""

    SECRET_FOR_TESTS = "test-orchestrator-secret"

    def __init__(self, body: Optional[dict] = None):
        from types import SimpleNamespace
        self._body = body or {}
        self.headers = {"x-orchestrator-secret": self.SECRET_FOR_TESTS}
        self.state = SimpleNamespace()
        self.query_params = {}

    async def json(self) -> dict:
        return self._body
