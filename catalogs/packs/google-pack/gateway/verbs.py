"""Argument handling of the 24 verbs: what the gateway's `_dispatch_gdrive`,
`_dispatch_gcalendar`, `_dispatch_gdocs` and `_dispatch_gsheets` did.

Moved from clodia-tools `server/main.py` (clodia-platform#558). The dispatcher
calls `handle(name, arguments)` at the point where it called those functions,
after the whitelist, deny, unattended block, gates and the egress PDP; taint
and audit follow it. Scratch paths are resolved by the HOST
(`host.scratch_path`: inside the calling spawn's scratch, or refused) and the
bytes that leave are hashed by the host (`host.note_crossing`), as the native
dispatcher did.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from . import _host
from . import gcalendar as gc
from . import gdocs as gdo
from . import gdrive as gd
from . import gsheets as gsh


def _gdrive(verb: str, a: dict) -> Any:
    host = _host.get()
    if verb == "list":
        return gd.list_files(folder_id=a.get("folder_id"), query=a.get("query"),
                             limit=a.get("limit", 50), account=a.get("account"))
    if verb == "search":
        return gd.search(a["name"], limit=a.get("limit", 20), account=a.get("account"))
    if verb == "mkdir":
        return gd.mkdir(a["name"], parent_id=a.get("parent_id"), account=a.get("account"))
    if verb == "upload":
        src = host.scratch_path(a["src"])   # the bytes come from the scratch, never the model
        host.note_crossing(path=src, kind="upload")
        return gd.upload(src, name=a.get("name"), folder_id=a.get("folder_id"),
                         account=a.get("account"))
    if verb == "update":
        src = host.scratch_path(a["src"])
        host.note_crossing(path=src, kind="upload")
        return gd.update(a["file_id"], src, account=a.get("account"))
    if verb == "download":
        dest = host.scratch_path(a["dest"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return gd.download(a["file_id"], dest, account=a.get("account"))
    if verb == "share":
        return gd.share(a["file_id"], a["email"], role=a.get("role", "writer"),
                        account=a.get("account"))
    if verb == "rename":
        return gd.rename(a["file_id"], a["new_name"], account=a.get("account"))
    if verb == "move":
        return gd.move(a["file_id"], a["folder_id"], account=a.get("account"))
    raise ValueError(f"unknown gdrive verb: gdrive.{verb}")


def _gcalendar(verb: str, a: dict) -> Any:
    if verb == "list_calendars":
        return gc.list_calendars(account=a.get("account"))
    if verb == "list_events":
        return gc.list_events(calendar_id=a.get("calendar_id", "primary"),
                              time_min=a.get("time_min"), time_max=a.get("time_max"),
                              query=a.get("query"), limit=a.get("limit", 25),
                              account=a.get("account"))
    if verb == "create_event":
        return gc.create_event(a["summary"], a["start"], a["end"],
                               calendar_id=a.get("calendar_id", "primary"),
                               description=a.get("description"), location=a.get("location"),
                               attendees=a.get("attendees"), all_day=a.get("all_day", False),
                               account=a.get("account"))
    if verb == "update_event":
        return gc.update_event(a["event_id"], calendar_id=a.get("calendar_id", "primary"),
                               summary=a.get("summary"), start=a.get("start"),
                               end=a.get("end"), description=a.get("description"),
                               location=a.get("location"), account=a.get("account"))
    if verb == "delete_event":
        return gc.delete_event(a["event_id"], calendar_id=a.get("calendar_id", "primary"),
                               account=a.get("account"))
    if verb == "freebusy":
        return gc.freebusy(a["time_min"], a["time_max"],
                           calendar_id=a.get("calendar_id", "primary"),
                           account=a.get("account"))
    raise ValueError(f"unknown gcalendar verb: gcalendar.{verb}")


def _gdocs(verb: str, a: dict) -> Any:
    if verb == "create":
        return gdo.create(a["title"], text=a.get("text"), account=a.get("account"))
    if verb == "read":
        return gdo.read(a["document_id"], account=a.get("account"))
    if verb == "append_text":
        return gdo.append_text(a["document_id"], a["text"], account=a.get("account"))
    if verb == "replace_text":
        return gdo.replace_text(a["document_id"], a["find"], a["replace"],
                                match_case=a.get("match_case", True), account=a.get("account"))
    raise ValueError(f"unknown gdocs verb: gdocs.{verb}")


def _gsheets(verb: str, a: dict) -> Any:
    if verb == "list_tabs":
        return gsh.list_tabs(a["spreadsheet_id"], account=a.get("account"))
    if verb == "read":
        return gsh.read(a["spreadsheet_id"], range=a.get("range"), tab=a.get("tab"),
                        formulas=bool(a.get("formulas")), account=a.get("account"))
    if verb == "add_tab":
        return gsh.add_tab(a["spreadsheet_id"], a["title"], index=a.get("index"),
                           account=a.get("account"))
    if verb == "append_rows":
        return gsh.append_rows(a["spreadsheet_id"], a["tab"], a["rows"],
                               account=a.get("account"))
    if verb == "write_range":
        return gsh.write_range(a["spreadsheet_id"], a["range"], a["values"],
                               account=a.get("account"))
    raise ValueError(f"unknown gsheets verb: gsheets.{verb}")


_BY_NAMESPACE: dict[str, Callable[[str, dict], Any]] = {
    "gdrive": _gdrive, "gcalendar": _gcalendar, "gdocs": _gdocs, "gsheets": _gsheets,
}


def handle(name: str, arguments: dict) -> Any:
    ns, _, verb = name.partition(".")
    fn = _BY_NAMESPACE.get(ns)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return fn(verb, dict(arguments or {}))


def handler_for(name: str) -> Callable[[dict], Any]:
    """The `register_verb` handler of `name` (a closure: the host passes only
    the arguments)."""
    return lambda arguments: handle(name, arguments)


def drive_parent_of(arguments: dict) -> str:
    """The folder that holds the file of `gdrive.update`, or "" when unknown.

    The gateway's `_drive_parent_of`, moved (service `pdp_arguments`): the
    destination of an update is the FOLDER of the file, read from Drive because
    the arguments only carry the file id. Unknown → the PDP sees an unknown
    destination → denied."""
    from . import google_io
    try:
        f = google_io.call(gd._resolve_account(arguments.get("account")), "drive", "v3",
                           "GET files/{fileId}", params={
                               "fileId": str(arguments.get("file_id") or ""),
                               "fields": "parents", "supportsAllDrives": True})
        return str((f.get("parents") or [""])[0])
    except Exception:  # noqa: BLE001 — unknown = denied by the verdict, not an error here
        return ""


def pdp_arguments(name: str, arguments: dict) -> dict:
    """Arguments the gateway adds for the egress PDP before the call (service).

    Only `gdrive.update` needs one; every other verb carries its destination."""
    if name == "gdrive.update":
        return {"folder_id": drive_parent_of(arguments)}
    return {}
