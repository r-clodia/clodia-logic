"""Test support (moved from clodia-tools `server/tools/_google_fake.py`, #524):
answer the google-pack component's `google_io` requests from a
googleapiclient-shaped fake.

The Google suites were written against fakes with the shape of a discovery
service (`svc.files().get(fileId=…).execute()`). Since clodia-platform#524 the
gateway calls `google_io.call(account, api, version, "GET files/{fileId}", …)`
instead. `ServiceTransport` is the bridge: it turns each transport request back
into the discovery method it stands for, with the same keyword arguments the
verbs used to pass, so the existing fakes and their assertions keep meaning
what they meant.

`OPERATIONS` is the 1:1 table between the REST form and the discovery method
name. `test_524_google_io` checks it against the discovery documents, so a
wrong path or HTTP method here is a failing test, not a silent mismatch.

Not a test module (no `test_` prefix): imported by the tests only.
"""
from __future__ import annotations

from typing import Any

#: (api, HTTP method, path template) → discovery method (`resource.method`).
OPERATIONS: dict[tuple[str, str, str], str] = {
    ("drive", "GET", "files"): "files.list",
    ("drive", "POST", "files"): "files.create",
    ("drive", "GET", "files/{fileId}"): "files.get",
    ("drive", "PATCH", "files/{fileId}"): "files.update",
    ("drive", "GET", "files/{fileId}/export"): "files.export",
    ("drive", "POST", "files/{fileId}/permissions"): "permissions.create",
    ("docs", "POST", "documents"): "documents.create",
    ("docs", "GET", "documents/{documentId}"): "documents.get",
    ("docs", "POST", "documents/{documentId}:batchUpdate"): "documents.batchUpdate",
    ("sheets", "GET", "spreadsheets/{spreadsheetId}"): "spreadsheets.get",
    ("sheets", "POST", "spreadsheets/{spreadsheetId}:batchUpdate"):
        "spreadsheets.batchUpdate",
    ("sheets", "GET", "spreadsheets/{spreadsheetId}/values/{range}"):
        "spreadsheets.values.get",
    ("sheets", "PUT", "spreadsheets/{spreadsheetId}/values/{range}"):
        "spreadsheets.values.update",
    ("sheets", "POST", "spreadsheets/{spreadsheetId}/values/{range}:append"):
        "spreadsheets.values.append",
    ("calendar", "GET", "users/me/calendarList"): "calendarList.list",
    ("calendar", "GET", "calendars/{calendarId}/events"): "events.list",
    ("calendar", "POST", "calendars/{calendarId}/events"): "events.insert",
    ("calendar", "GET", "calendars/{calendarId}/events/{eventId}"): "events.get",
    ("calendar", "PUT", "calendars/{calendarId}/events/{eventId}"): "events.update",
    ("calendar", "DELETE", "calendars/{calendarId}/events/{eventId}"): "events.delete",
    ("calendar", "POST", "freeBusy"): "freebusy.query",
}

#: Discovery methods whose media form had its own name in the client.
_MEDIA_NAMES = {"files.get": "files.get_media", "files.export": "files.export_media"}


class FakeMedia:
    """What a fake sees as `media_body`: the `MediaFileUpload` surface it used."""

    def __init__(self, media):
        self._media = media
        self._filename = media.path

    def mimetype(self) -> str:
        return self._media.content_type()


def operation(req: dict) -> str:
    """The discovery method a transport request stands for."""
    key = (req["api"], req["method"], req["template"])
    if key not in OPERATIONS:
        raise AssertionError(f"no discovery method for {key}")
    op = OPERATIONS[key]
    if req.get("download"):
        op = _MEDIA_NAMES.get(op, op)
    return op


def kwargs(req: dict) -> dict:
    """The keyword arguments the discovery client would have been given."""
    kw: dict[str, Any] = {**req["path_params"], **req["params"]}
    if req["body"] is not None:
        kw["body"] = req["body"]
    if req["media"] is not None:
        kw["media_body"] = FakeMedia(req["media"])
    return kw


class ServiceTransport:
    """A `google_io._transport` answering from one fake service object, or
    from one per API (`{"drive": fake, "sheets": fake, …}`)."""

    def __init__(self, fake):
        self.fake = fake
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, account: str, req: dict):
        self.requests.append((account, req))
        svc = self.fake.get(req["api"]) if isinstance(self.fake, dict) else self.fake
        target = svc
        *resources, method = operation(req).split(".")
        for name in resources:
            target = getattr(target, name)()
        return getattr(target, method)(**kwargs(req)).execute()
