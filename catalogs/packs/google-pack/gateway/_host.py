"""The host this component was registered with, and the credential helpers.

`register(host)` (google_component.py) stores the host here; every other
module of the component reaches the gateway ONLY through it — the import guard
of the gateway refuses `server.*` imports, and the component has no business
with them: credentials come through `host.credentials(prefix)`, the perimeter
through `host.perimeter`, the call's facts through `host.channel()`.

Credential scopes. The component declares four entries (`pack.yaml`,
`components[0].credentials`): the prefixes `google_`, `gworkspace_`, `gmail_`
and the exact entry `app_google_oauth`. They stay the gateway's custody
(`builtin.CORE_CREDENTIALS_FOLLOW`): reachable only while the instance switch
is `connectors: {google: pack}`, checked again at every access, so turning
the switch back revokes them at once. Nothing is copied: no re-consent.
"""
from __future__ import annotations

from typing import Any

HOST: Any = None

#: Exact vault entry of the OAuth app client (client id/secret, redirect).
APP_CREDENTIAL = "app_google_oauth"
#: The vault prefixes of Google credentials, in the order the verbs prefer them.
PREFIXES = ("google_", "gworkspace_", "gmail_")


def get() -> Any:
    if HOST is None:
        raise RuntimeError("google-pack component: no host (register() not called)")
    return HOST


def _scope(name: str):
    """The `ScopedCredentials` that covers the vault entry `name`."""
    if name == APP_CREDENTIAL:
        return get().credentials(APP_CREDENTIAL)
    for prefix in PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return get().credentials(prefix)
    raise PermissionError(f"'{name}' is not a Google credential of this component")


def has_credential(name: str) -> bool:
    return bool(_scope(name).has(name))


def names(prefix: str) -> list[str]:
    """Vault entry names (never values) under `prefix`."""
    return list(get().credentials(prefix).list())


def read_internal(name: str) -> dict:
    """The bundle, read as gateway infrastructure (audited as `system`)."""
    return _scope(name).read_internal(name)


def deposit(name: str, bundle: dict, *, cred_type: str, grant_agents=()) -> None:
    _scope(name).deposit(name, bundle, cred_type=cred_type,
                         grant_agents=list(grant_agents))
