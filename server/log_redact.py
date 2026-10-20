"""Access-log redaction (clodia-platform#511).

Uvicorn's access log writes the full request URL. A credential carried in a
query string — the session token of the live stream used to be — ends up in
clear in `docker logs`, log shipping, support bundles and any inference
context that reads them. The live stream no longer accepts a token in the
URL; this filter is the defence in depth: whatever a future client puts in a
query string under a sensitive name is written as `***`.
"""
from __future__ import annotations

import logging
import re

#: Query parameter names whose value is never written (case-insensitive): any
#: name ENDING in a credential word (`x-api-key`, `secret_key`, `db_password`,
#: `X-Amz-Signature`, `access_token`, …), plus an OAuth `code`. A useful
#: parameter that happens to end that way (`?key=theme`) is masked too: a log
#: line that hides a harmless value is cheaper than one that keeps a secret.
SENSITIVE = re.compile(
    r"(?i)([?&](?:[a-z0-9_.%-]*(?:token|secret|password|passwd|pwd|pass|key|"
    r"signature|sig|credential|session|sid|jwt|auth|authorization)|code)=)"
    r"[^&#\s\"']*")


def redact(text: str) -> str:
    """`/clodia/events?token=ckt1.abc&x=1` → `/clodia/events?token=***&x=1`."""
    return SENSITIVE.sub(r"\1***", text or "")


class RedactQueryFilter(logging.Filter):
    """Rewrites the access-log record's arguments before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple):
                record.args = tuple(redact(a) if isinstance(a, str) else a
                                    for a in record.args)
            elif isinstance(record.msg, str):
                record.msg = redact(record.msg)
        except Exception:  # noqa: BLE001 — a log line must never fail the request
            pass
        return True


def install(logger_names: tuple[str, ...] = ("uvicorn.access",)) -> None:
    """Attach the filter once to each access logger (idempotent)."""
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactQueryFilter) for f in logger.filters):
            logger.addFilter(RedactQueryFilter())
