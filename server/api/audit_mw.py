"""Control-plane and session events of the agent-server, from ONE middleware
(clodia-platform#439 part 2, #444 part 2).

The gateway records the rules it holds (whitelists, grants, vault, PKI,
participants, re-classification). The agent-server holds others: providers
and their order/pause/credentials, packs, plugins and skills, agent seeds
(human seeds included, i.e. roles), agent profiles, pause/resume, human
certificate requests, datastore purges, channel aliases, observe-mode
whitelists. Instrumenting each endpoint would leave the next one out, so a
middleware records every MUTATING request on those surfaces:

    control.<kind>  actor = the verified person (or signed agent), role,
                    method, route template, path parameters, status code,
                    NAMES of the query keys — never bodies or values.

A person's session is recorded once per session token (`human.session`,
keyed by the token's signed `iat`): the webui signs its own session token with
the person's certificate and there is no server-side login to hook, so the
first verified request of a token is the session start.
"""
from __future__ import annotations

import threading

#: (path prefix, kind). Order matters: the first match wins.
CONTROL_SURFACES = (
    ("/api/providers", "provider"),
    ("/auth/", "provider_login"),
    ("/clodia/packs", "pack"),
    ("/clodia/plugins", "plugin"),
    ("/clodia/skills", "skill"),
    ("/clodia/agents/", "agent_profile"),
    ("/api/agents", "agent"),
    ("/clodia/runtime/restart-agent", "runtime"),
    ("/api/observe/whitelist", "observe_whitelist"),
    ("/clodia/cert-request", "pki"),
    ("/api/cert-requests", "pki"),
    ("/clodia/datastores", "datastore"),
    ("/api/channel-aliases", "alias"),
    ("/api/admin", "admin"),
)
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_sessions: set[tuple[str, int]] = set()
_lock = threading.Lock()
_MAX_SESSIONS = 10_000


def kind_of(method: str, path: str) -> str | None:
    if method not in MUTATING:
        return None
    for prefix, kind in CONTROL_SURFACES:
        if path.startswith(prefix):
            return kind
    return None


def actor_of(claims: dict | None) -> dict:
    if not claims:
        return {"type": "anonymous"}
    if claims.get("on_behalf") and claims.get("principal"):
        return {"type": "human", "id": claims["principal"], "role": claims.get("human_role"),
                "via": claims.get("agent")}
    # A webui session token is signed by the person's own certificate: `agent`
    # IS the person there.
    return {"type": "principal", "id": claims.get("agent"), "role": claims.get("human_role")}


def control_event(kind: str, request, status: int, claims: dict | None) -> dict:
    route = request.scope.get("route")
    template = getattr(route, "path", None) or request.url.path
    return {"type": f"control.{kind}", "action": request.method.lower(),
            "resource": template, "actor": actor_of(claims),
            "result": {"status_code": status,
                       "path_params": dict(request.path_params) or None,
                       "query_keys": sorted(request.query_params.keys()) or None}}


def session_event(claims: dict | None) -> dict | None:
    """The first verified request of a person's session token, else None."""
    if not claims or not claims.get("agent") or claims.get("execution_id"):
        return None  # anonymous, or an agent's own token
    key = (str(claims.get("principal") or claims.get("agent")), int(claims.get("iat") or 0))
    with _lock:
        if key in _sessions:
            return None
        if len(_sessions) >= _MAX_SESSIONS:
            _sessions.clear()
        _sessions.add(key)
    return {"type": "human.session", "action": "start", "resource": "webui",
            "actor": actor_of(claims),
            "result": {"issued_at": claims.get("iat"), "expires_at": claims.get("exp")}}


def install(app) -> None:
    from starlette.requests import Request  # noqa: F401 - typing only

    @app.middleware("http")
    async def _audit_control_plane(request, call_next):
        response = await call_next(request)
        try:
            kind = kind_of(request.method, request.url.path)
            auth = request.headers.get("authorization", "")
            claims = None
            if kind or auth.lower().startswith("bearer "):
                from .agents import _verified_claims
                claims = _verified_claims(request) if auth else None
            from .. import audit_events
            sess = session_event(claims)
            if sess:
                audit_events._bg(sess)
            if kind:
                audit_events._bg(control_event(kind, request, response.status_code, claims))
        except Exception:  # noqa: BLE001 - auditing never changes the response
            pass
        return response
