"""Canali di contatto derivati per agent.

Regole (decise con owner):
- super (clodia/ophelia): email + telegram da campi espliciti; in mancanza,
  clodia = mailbox reale, gli altri super = subaddress della mailbox reale.
- regular (AI normale): email = subaddress della mailbox del super genitore
  (mailbox_parent, default "clodia"); nessun telegram dedicato.
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


def _super_email(spec) -> str:
    if getattr(spec, "email", None):
        return spec.email
    if spec.name == "clodia":
        return BASE_EMAIL
    bl, dom = _split(BASE_EMAIL)
    return f"{bl}+{spec.name}@{dom}"


def channels(spec) -> dict:
    """Ritorna {email, telegram} per l'agent."""
    t = getattr(spec, "type", "normal")
    bl, dom = _split(BASE_EMAIL)
    if t == "super":
        return {"email": _super_email(spec), "telegram": getattr(spec, "telegram", None)}
    if t == "human":
        return {"email": getattr(spec, "email", None),
                "telegram": getattr(spec, "telegram", None)}
    # regular: subaddress del super genitore
    if getattr(spec, "email", None):
        return {"email": spec.email, "telegram": None}
    parent = (getattr(spec, "mailbox_parent", None) or "clodia").lower()
    tag = spec.name if parent == "clodia" else f"{parent}-{spec.name}"
    return {"email": f"{bl}+{tag}@{dom}", "telegram": None}
