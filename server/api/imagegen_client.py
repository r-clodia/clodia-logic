"""Client verso l'endpoint interno di generazione immagini del gateway.

Stessa logica di `provider_store`: il runner di clodia-logic chiama il gateway
clodia-tools (`POST /internal/imagegen`) con un token ckt1 firmato per il
principal `clodia`. La OpenAI key vive solo nel gateway (vault); qui passa solo
prompt/immagine e torna il PNG.
"""
from __future__ import annotations

import os

import requests

from ..colony import pki

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 300  # la generazione immagine può richiedere parecchi secondi


class ImageGenError(RuntimeError):
    """Errore di rete/gateway nella generazione immagine."""


class ImageGenUnavailable(ImageGenError):
    """L'integrazione OpenAI non è attiva sul gateway (nessuna key)."""


def _url() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_IMAGEGEN_URL")
    if explicit:
        return explicit
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/").rstrip("/")
    if mcp.endswith("/mcp"):
        mcp = mcp[: -len("/mcp")]
    return f"{mcp}/internal/imagegen"


def generate(prompt: str, image_b64: str | None = None) -> bytes:
    """Ritorna i byte PNG. Se `image_b64` è fornito → image→image (restyle)."""
    token = pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)
    payload: dict = {"prompt": prompt}
    if image_b64:
        payload["image_b64"] = image_b64
    try:
        r = requests.post(_url(), headers={"Authorization": f"Bearer {token}"},
                          json=payload, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ImageGenError(f"gateway imagegen irraggiungibile: {e}") from e
    if r.status_code == 409:
        raise ImageGenUnavailable(
            "integrazione Image generation non attiva: collega la OpenAI key "
            "nella sezione Integrazioni")
    if r.status_code != 200:
        raise ImageGenError(f"gateway imagegen → HTTP {r.status_code}: {r.text[:200]}")
    return r.content
