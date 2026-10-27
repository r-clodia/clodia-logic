"""What an agent may do, asked of the gateway instead of read from its seed
(clodia-platform#626).

The seeds no longer carry verbs: under A·V·R·Q the gateway's tuples decide
(the room's, and the global ones — governance everywhere, MCPs outside rooms).
The agent-server still needs the answer in a few places that are not
authorisation but behaviour — whether a room's coordinator can answer on a
channel, whether an agent may administer others, the static trifecta — so it
asks, with a short memory.

Two calls, both cached:

- `has(agent, verb, scope)` → True/False, or None when the gateway has never
  answered for that question (the caller chooses its own direction of error);
- `global_grants(agent)` → the verb patterns the global tuples give `agent`
  (and `*`), or None when the gateway does not answer.

A failure never raises here: these are signals for behaviour, and the gateway
still decides every call.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

LOG = logging.getLogger("agent-server.authz_grants")

#: How long an answer is fresh, and how long a stale one is still used when the
#: gateway does not answer (a transient error must not flip a behaviour).
TTL = 60.0
STALE_OK = 3600.0

#: After a failure, the gateway is not asked again for this long: a mute
#: gateway must not stall the relay or a page with one timeout per question.
BACKOFF = 30.0

_lock = threading.Lock()
_has: dict[tuple, tuple[float, bool]] = {}
_grants: dict[str, tuple[float, list[str]]] = {}
_mute_until = 0.0


def clear() -> None:
    global _mute_until
    with _lock:
        _has.clear()
        _grants.clear()
        _mute_until = 0.0


def _mute() -> bool:
    return time.monotonic() < _mute_until


def _failed() -> None:
    global _mute_until
    with _lock:
        _mute_until = time.monotonic() + BACKOFF


def has(agent: str, verb: str, scope: Optional[str] = None) -> Optional[bool]:
    """Does `agent` have `verb` in `scope` (None = outside a room)?"""
    if not agent or not verb:
        return False
    key = (agent, verb, scope or "")
    now = time.monotonic()
    with _lock:
        hit = _has.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    try:
        if _mute():
            raise TimeoutError("gateway muto di recente")
        from ..api import topics_client
        ans = bool(topics_client.authz_available(agent, [verb], scope).get(verb))
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, TimeoutError) or not _mute():
            _failed()
        if hit and now - hit[0] < STALE_OK:
            LOG.info("authz: gateway muto per %s/%s, uso l'ultimo esito (%s)", agent, verb,
                     type(e).__name__)
            return hit[1]
        (LOG.debug if isinstance(e, TimeoutError) else LOG.warning)(
            "authz: %s/%s non determinabile (%s)", agent, verb, type(e).__name__)
        return None
    with _lock:
        _has[key] = (now, ans)
    return ans


def _gives(t: dict) -> bool:
    """A tuple gives its verb unless every clause denies."""
    rule = t.get("rule") or []
    return any(str((c or {}).get("effect") or "") != "deny" for c in rule)


def global_grants(agent: str) -> Optional[list[str]]:
    """The verb patterns the GLOBAL tuples give `agent` (its own and `*`'s)."""
    if not agent:
        return []
    now = time.monotonic()
    with _lock:
        hit = _grants.get(agent)
    if hit and now - hit[0] < TTL:
        return list(hit[1])
    try:
        if _mute():
            raise TimeoutError("gateway muto di recente")
        from ..api import topics_client
        rows = (topics_client.authz_tuples(agent) or {}).get("tuples") or []
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, TimeoutError) or not _mute():
            _failed()
        if hit and now - hit[0] < STALE_OK:
            return list(hit[1])
        (LOG.debug if isinstance(e, TimeoutError) else LOG.warning)(
            "authz: tuple globali di %s non disponibili (%s)", agent, type(e).__name__)
        return None
    verbs = sorted({str(t.get("verb") or "") for t in rows
                    if str(t.get("agent") or "") in (agent, "*") and _gives(t)
                    and str(t.get("verb") or "")})
    with _lock:
        _grants[agent] = (now, verbs)
    return list(verbs)
