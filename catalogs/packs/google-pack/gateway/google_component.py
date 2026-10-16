"""google-pack's gateway component: Google Workspace, run inside the gateway.

clodia-platform#558 (owner decision 2026-10-16): a pack is a distribution of
components, each mounted in its container. This is the `target: gateway` one.
The gateway fetches it from r-clodia/clodia-logic (default branch, pinned
commit), imports this module and calls `register(host)` — and, from then on,
on `connectors: {google: pack}`:

- the 24 `gdrive/gsheets/gdocs/gcalendar` verbs are served from here, through
  the gateway's normal `call_tool` path (whitelist, deny, unattended block,
  gates, the egress PDP on the mappings of this pack's manifest, taint, audit);
- the folder/calendar perimeter (`gdrive_root`) runs here, reading the
  perimeter from the gateway per call (`host.perimeter`, `host.channel()`);
- the OAuth consent and the app client are this component's routes
  (`/tools/google-pack/{app,auth,connect}`; the gateway's `/tools/google/*`
  forward to them), and the credentials stay in the gateway's vault, reached
  through `host.credentials(...)` — no re-consent;
- the gateway calls back the services below where its own code needs Google:
  the confinement API, the labels of a whitelist, the pre-PDP parent lookup of
  `gdrive.update`, the Tools card test, the token cache, and Gmail for the
  `email.*` verbs of a Google account.

On `native` (the default) the gateway keeps this component pinned but on
standby: none of this runs.
"""
from __future__ import annotations

from typing import Any

from . import _host

#: The services the gateway calls (`components.service("google-pack", name)`).
SERVICES = ("accounts", "describe_folder", "perimeter_changed", "credential_changed",
            "folder_labels", "pdp_arguments", "test_connection", "mail_run")


def _accounts() -> list[str]:
    from . import gdrive
    return gdrive.gworkspace_accounts()


def _perimeter_changed() -> None:
    """The perimeter moved (confinement API): drop the parent and topic caches."""
    from . import gdrive_root
    gdrive_root.reset_cache()
    gdrive_root.reset_topic_cache()


def _credential_changed(name: str = "") -> None:
    """A Google credential was deposited or removed: drop the cached tokens."""
    from . import google_io
    google_io.forget()


def _folder_labels(uris: list, budget_s: float | None = None) -> dict:
    from . import gdrive_labels
    if budget_s is None:
        return gdrive_labels.labels(list(uris or []))
    return gdrive_labels.labels(list(uris or []), budget_s=budget_s)


def register(host: Any) -> None:
    _host.HOST = host
    # Every module imported HERE, not at first use: an import error disables
    # the component at load (logged, its verbs denied), not on a live call.
    from . import (gcalendar, gdocs, gdrive, gdrive_labels, gdrive_root,  # noqa: F401
                   gmail_core, google_io, google_oauth, gsheets, routes, schemas,
                   verbs)

    for tool in schemas.TOOLS:
        host.register_verb(tool, verbs.handler_for(tool.name))
    for path, methods, handler, auth in routes.ROUTES:
        host.register_route(path, methods, handler, auth=auth)
    host.register_service("accounts", _accounts)
    host.register_service("describe_folder", routes.describe_folder)
    host.register_service("perimeter_changed", _perimeter_changed)
    host.register_service("credential_changed", _credential_changed)
    host.register_service("folder_labels", _folder_labels)
    host.register_service("pdp_arguments", verbs.pdp_arguments)
    host.register_service("test_connection", routes.test_connection)
    host.register_service("mail_run", gmail_core.run)
