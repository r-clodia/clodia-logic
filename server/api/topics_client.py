"""Client verso gli endpoint interni dei topic del gateway (Topic System v2).

Stesso pattern di provider_store/imagegen_client: il runner di clodia-logic fa da
proxy per la webui, chiamando il gateway (`/internal/topics`) con un token ckt1
firmato per il principal `clodia`. I topic v2 vivono dietro il gateway; qui li
leggiamo solo per servirli alla pagina Topics.
"""
from __future__ import annotations

import os

import requests

from ..colony import pki

_PRINCIPAL = os.environ.get("CLODIA_PROVIDER_PRINCIPAL", "clodia")
_TOKEN_TTL = 300
_HTTP_TIMEOUT = 15


class TopicsClientError(RuntimeError):
    pass


def _base() -> str:
    explicit = os.environ.get("CLODIA_TOOLS_TOPICS_URL")
    if explicit:
        return explicit.rstrip("/")
    mcp = os.environ.get("CLODIA_TOOLS_MCP_URL", "http://clodia-tools:7849/mcp/").rstrip("/")
    if mcp.endswith("/mcp"):
        mcp = mcp[: -len("/mcp")]
    return f"{mcp}/internal/topics"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {pki.mint_session_token(_PRINCIPAL, ttl_seconds=_TOKEN_TTL)}"}


def list_topics(tier: str | None = None, include_archived: bool = False) -> list[dict]:
    params = {}
    if tier:
        params["tier"] = tier
    if include_archived:
        params["include_archived"] = "true"
    try:
        r = requests.get(_base(), headers=_headers(), params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway topics irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway topics → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("topics", [])


def open_topic(tier: str, name: str) -> dict | None:
    url = f"{_base()}/{tier}/{name}"
    try:
        r = requests.get(url, headers=_headers(), timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway topics irraggiungibile: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise TopicsClientError(f"gateway topic open → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def create_topic(tier: str, name: str, meta: dict) -> dict:
    try:
        r = requests.post(_base(), headers=_headers(),
                          json={"tier": tier, "name": name, "meta": meta},
                          timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway create_topic irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway create_topic → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("meta", {})


def list_messages(tier: str, name: str, limit: int = 200) -> list[dict]:
    url = f"{_base()}/{tier}/{name}/messages"
    try:
        r = requests.get(url, headers=_headers(), params={"limit": limit}, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway messages irraggiungibile: {e}") from e
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise TopicsClientError(f"gateway messages → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("messages", [])


def post_message(tier: str, name: str, author: str, text: str,
                 kind: str = "human", attachments: list[str] | None = None) -> dict:
    url = f"{_base()}/{tier}/{name}/messages"
    body = {"author": author, "text": text, "kind": kind, "attachments": attachments or []}
    try:
        r = requests.post(url, headers=_headers(), json=body, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway post_message irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway post_message → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def set_participant(tier: str, name: str, agent: str, add: bool = True) -> dict:
    url = f"{_base()}/{tier}/{name}/participants"
    method = requests.post if add else requests.delete
    try:
        r = method(url, headers=_headers(), json={"agent": agent}, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway participants irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway participants → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def list_files(tier: str, name: str, subpath: str = "") -> list[dict]:
    url = f"{_base()}/{tier}/{name}/files"
    try:
        r = requests.get(url, headers=_headers(), params={"path": subpath},
                         timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway files irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway files → HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("files", [])


def put_file(tier: str, name: str, filename: str, content_b64: str) -> dict:
    url = f"{_base()}/{tier}/{name}/files"
    try:
        r = requests.post(url, headers=_headers(),
                          json={"filename": filename, "content_b64": content_b64},
                          timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway put_file irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway put_file → HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def export_bundle(topics: list[str] | None = None) -> bytes:
    """Scarica dal gateway il tar.gz dei topic (snapshot). `topics` = lista di
    'tier/name' da includere; None → tutti."""
    url = f"{_base()}/export"
    params = {"topics": ",".join(topics)} if topics else None
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=300)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway export irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway export → HTTP {r.status_code}: {r.text[:160]}")
    return r.content


def import_bundle(data: bytes) -> dict:
    """Invia al gateway il tar.gz da importare (merge non-distruttivo)."""
    url = f"{_base()}/import"
    headers = {**_headers(), "Content-Type": "application/gzip"}
    try:
        r = requests.post(url, headers=headers, data=data, timeout=300)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway import irraggiungibile: {e}") from e
    if r.status_code != 200:
        raise TopicsClientError(f"gateway import → HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def get_file(tier: str, name: str, path: str) -> bytes | None:
    """Byte di un file dentro il topic (es. files/foo.md), via gateway. None se 404."""
    url = f"{_base()}/{tier}/{name}/file"
    try:
        r = requests.get(url, headers=_headers(), params={"path": path}, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TopicsClientError(f"gateway topic file irraggiungibile: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise TopicsClientError(f"gateway topic file → HTTP {r.status_code}: {r.text[:160]}")
    return r.content
