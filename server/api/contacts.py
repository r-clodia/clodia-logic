"""Canali di contatto derivati per agent.

Regole (decise con owner):
- clodia: email + telegram da campi espliciti; in mancanza email = mailbox reale.
- altri bot: email = subaddress della mailbox del parent (mailbox_parent,
  default "clodia") salvo campo esplicito.
- human: email + telegram dai campi (popolati alla creazione / cert-request).

La mailbox reale è una sola (Clodia); gli altri canali sono subaddress (+tag)
per restare deliverable su Gmail (un solo '+').
"""
from __future__ import annotations

import os

# Mailbox reale dell'agency, configurata dall'owner via env CLODIA_BASE_EMAIL.
# Default placeholder non funzionante: ogni deployment deve impostarla (i canali
# di contatto via subaddressing dipendono da questo valore).
BASE_EMAIL = os.environ.get("CLODIA_BASE_EMAIL", "agency@example.com")


def _split(addr: str) -> tuple[str, str]:
    local, _, domain = (addr or "").partition("@")
    return local.split("+", 1)[0], domain


def channels(spec) -> dict:
    """Ritorna {email, telegram} per l'agent."""
    t = getattr(spec, "type", "bot")
    bl, dom = _split(BASE_EMAIL)
    if t == "human":
        return {"email": getattr(spec, "email", None),
                "telegram": getattr(spec, "telegram", None)}
    if getattr(spec, "email", None):
        return {"email": spec.email, "telegram": getattr(spec, "telegram", None)}
    if spec.name == "clodia":
        return {"email": BASE_EMAIL, "telegram": getattr(spec, "telegram", None)}
    parent = (getattr(spec, "mailbox_parent", None) or "clodia").lower()
    tag = spec.name if parent == "clodia" else f"{parent}-{spec.name}"
    return {"email": f"{bl}+{tag}@{dom}", "telegram": getattr(spec, "telegram", None)}
