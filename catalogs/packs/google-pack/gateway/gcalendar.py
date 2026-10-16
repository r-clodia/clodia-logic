"""gcalendar.* — Google Calendar via MCP, sulla credenziale Workspace del vault.

Stessa credenziale di gdrive/gdocs (scope `calendar` già incluso). L'agente deve
avere un grant Workspace (`google_`/`gworkspace_`) — verificato dal vault.
Verbi: list_calendars, list_events, create_event, update_event, delete_event,
freebusy. Tutti gli orari sono ISO 8601 (RFC3339), es. 2026-07-22T15:00:00+02:00.

Se la credenziale è confinata a una cartella di Drive, questi verbi valgono solo
sui calendari dichiarati in whitelist come `gcal:<calendar_id>`: vedi
`gdrive_root.assert_calendar_allowed` (clodia-platform#429).
"""
from __future__ import annotations

from typing import Optional

from . import gdrive_root
from . import google_io
from . import gdrive


def _cal(acct: str, method_path: str, **kw) -> dict:
    """One Calendar v3 call for a resolved account, through the Google seam (#524)."""
    return google_io.call(acct, "calendar", "v3", method_path, **kw)


def list_calendars(account: Optional[str] = None) -> dict:
    gdrive_root.guard_calendar(account, "gcalendar.list_calendars")
    acct = gdrive._resolve_account(account)
    items = _cal(acct, "GET users/me/calendarList").get("items", [])
    cals = [{"id": c.get("id"), "summary": c.get("summary"),
             "primary": c.get("primary", False), "accessRole": c.get("accessRole")}
            for c in items]
    # Su credenziale confinata l'elenco si riduce ai calendari dichiarati: il
    # guard sopra ha solo accertato che ce ne sia almeno uno.
    cals = gdrive_root.keep_calendars(acct, cals)
    return {"account": acct, "calendars": cals}


def list_events(calendar_id: str = "primary", time_min: Optional[str] = None,
                time_max: Optional[str] = None, query: Optional[str] = None,
                limit: int = 25, account: Optional[str] = None) -> dict:
    gdrive_root.guard_calendar(account, "gcalendar.list_events", calendar_id)
    acct = gdrive._resolve_account(account)
    params = {"calendarId": calendar_id, "singleEvents": True, "orderBy": "startTime",
              "maxResults": max(1, min(int(limit or 25), 250))}
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    if query:
        params["q"] = query
    res = _cal(acct, "GET calendars/{calendarId}/events", params=params)
    ev = [_clean_event(e) for e in res.get("items", [])]
    return {"account": acct, "calendar_id": calendar_id, "events": ev}


def create_event(summary: str, start: str, end: str, calendar_id: str = "primary",
                 description: Optional[str] = None, location: Optional[str] = None,
                 attendees: Optional[list] = None, all_day: bool = False,
                 account: Optional[str] = None) -> dict:
    gdrive_root.guard_calendar(account, "gcalendar.create_event", calendar_id)
    acct = gdrive._resolve_account(account)
    body = {"summary": summary}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if all_day:
        body["start"] = {"date": start}
        body["end"] = {"date": end}
    else:
        body["start"] = {"dateTime": start}
        body["end"] = {"dateTime": end}
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    e = _cal(acct, "POST calendars/{calendarId}/events",
             params={"calendarId": calendar_id}, body=body)
    return {"account": acct, "event": _clean_event(e)}


def update_event(event_id: str, calendar_id: str = "primary",
                 summary: Optional[str] = None, start: Optional[str] = None,
                 end: Optional[str] = None, description: Optional[str] = None,
                 location: Optional[str] = None, account: Optional[str] = None) -> dict:
    gdrive_root.guard_calendar(account, "gcalendar.update_event", calendar_id)
    acct = gdrive._resolve_account(account)
    ids = {"calendarId": calendar_id, "eventId": event_id}
    e = _cal(acct, "GET calendars/{calendarId}/events/{eventId}", params=dict(ids))
    if summary is not None:
        e["summary"] = summary
    if description is not None:
        e["description"] = description
    if location is not None:
        e["location"] = location
    if start is not None:
        key = "date" if "date" in e.get("start", {}) else "dateTime"
        e["start"] = {key: start}
    if end is not None:
        key = "date" if "date" in e.get("end", {}) else "dateTime"
        e["end"] = {key: end}
    upd = _cal(acct, "PUT calendars/{calendarId}/events/{eventId}",
               params=dict(ids), body=e)
    return {"account": acct, "event": _clean_event(upd)}


def delete_event(event_id: str, calendar_id: str = "primary",
                 account: Optional[str] = None) -> dict:
    gdrive_root.guard_calendar(account, "gcalendar.delete_event", calendar_id)
    acct = gdrive._resolve_account(account)
    _cal(acct, "DELETE calendars/{calendarId}/events/{eventId}",
         params={"calendarId": calendar_id, "eventId": event_id})
    return {"account": acct, "deleted": event_id, "ok": True}


def freebusy(time_min: str, time_max: str, calendar_id: str = "primary",
             account: Optional[str] = None) -> dict:
    gdrive_root.guard_calendar(account, "gcalendar.freebusy", calendar_id)
    acct = gdrive._resolve_account(account)
    body = {"timeMin": time_min, "timeMax": time_max, "items": [{"id": calendar_id}]}
    res = _cal(acct, "POST freeBusy", body=body)
    busy = res.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return {"account": acct, "calendar_id": calendar_id, "busy": busy}


def _clean_event(e: dict) -> dict:
    return {k: e.get(k) for k in ("id", "summary", "description", "location",
                                  "start", "end", "status", "htmlLink", "attendees")
            if e.get(k) is not None}
