"""gsheets.* — Google Sheets via MCP, on the vault's Workspace credential.

Why this module exists at all (clodia-platform#118). Before it, the only way an
agent could act on a spreadsheet was `gdrive.download` + `gdrive.upload`, which
replaces the whole file: every tab, formula and per-tab format the agent did not
author is destroyed. A spreadsheet is not a blob, so the file-level connector is
the wrong granularity for it. Every verb here is INCREMENTAL — it preserves what
it does not name, by construction rather than by care.

No new consent was needed: the Workspace connector already requests
`https://www.googleapis.com/auth/drive`, which the Sheets API accepts. The
agent must hold the Workspace grant, as for gdrive/gdocs/gcalendar.

Verbs: list_tabs, read (values of a tab or A1 range, `formulas=True` for the
formula text), add_tab, append_rows, write_range. `append_rows` is the safe
write and covers most tasks; `write_range` is the only verb that can overwrite
existing cells, so it demands an explicit range and never defaults to one.
"""
from __future__ import annotations

from typing import Any, Optional

from . import gdrive_root
from . import google_io
from . import gdrive

_URL = "https://docs.google.com/spreadsheets/d/{}/edit"


def _sheets(acct: str, method_path: str, **kw) -> dict:
    """One Sheets v4 call for a resolved account, through the Google seam (#524)."""
    return google_io.call(acct, "sheets", "v4", method_path, **kw)


def _tabs(acct: str, spreadsheet_id: str) -> list[dict]:
    meta = _sheets(acct, "GET spreadsheets/{spreadsheetId}", params={
        "spreadsheetId": spreadsheet_id,
        "fields": "sheets.properties(title,sheetId,index,gridProperties)"})
    out = []
    for s in meta.get("sheets", []):
        p = s.get("properties", {})
        g = p.get("gridProperties", {})
        out.append({"title": p.get("title"), "sheet_id": p.get("sheetId"),
                    "index": p.get("index"), "rows": g.get("rowCount"),
                    "cols": g.get("columnCount")})
    return out


def list_tabs(spreadsheet_id: str, account: Optional[str] = None) -> dict:
    """Tabs of a spreadsheet: title, id, position and size."""
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, spreadsheet_id, "gsheets")
    meta = _sheets(acct, "GET spreadsheets/{spreadsheetId}", params={
        "spreadsheetId": spreadsheet_id, "fields": "properties.title"})
    tabs = _tabs(acct, spreadsheet_id)
    return {"account": acct, "spreadsheet_id": spreadsheet_id,
            "title": meta.get("properties", {}).get("title"),
            "tabs": tabs, "url": _URL.format(spreadsheet_id)}


def read(spreadsheet_id: str, range: Optional[str] = None,  # noqa: A002 - MCP arg name
         tab: Optional[str] = None, formulas: bool = False,
         account: Optional[str] = None) -> dict:
    """Values of an A1 `range`, or of a whole `tab`.

    With neither, reads the FIRST tab rather than guessing a name — an explicit
    read of something that exists beats an error on a spreadsheet whose tab
    names the agent has not looked up yet.

    `formulas=True` returns the formula TEXT (`=SUM(B1:C1)`) instead of the
    computed value. Without it a read is lossy in a way that matters here: an
    agent reading a budget in order to reproduce it elsewhere would get `0`
    where a formula lives, and write back a constant — destroying exactly what
    this module exists to preserve.
    """
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, spreadsheet_id, "gsheets")
    rng = range or tab
    if not rng:
        tabs = _tabs(acct, spreadsheet_id)
        if not tabs:
            return {"account": acct, "spreadsheet_id": spreadsheet_id,
                    "range": None, "values": [], "rows": 0}
        rng = tabs[0]["title"]
    res = _sheets(acct, "GET spreadsheets/{spreadsheetId}/values/{range}", params={
        "spreadsheetId": spreadsheet_id, "range": rng,
        "valueRenderOption": "FORMULA" if formulas else "FORMATTED_VALUE"})
    values = res.get("values", [])
    return {"account": acct, "spreadsheet_id": spreadsheet_id,
            "range": res.get("range", rng), "values": values, "rows": len(values),
            "formulas": bool(formulas)}


def add_tab(spreadsheet_id: str, title: str, index: Optional[int] = None,
            account: Optional[str] = None) -> dict:
    """Adds a tab to an EXISTING spreadsheet, leaving the others untouched.

    This is the operation the file-level connector could not express. `addSheet`
    is a mutation of the spreadsheet, not a rewrite of the file: the other tabs
    are not read, not sent and not rewritten.
    """
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, spreadsheet_id, "gsheets")
    props: dict[str, Any] = {"title": title}
    if index is not None:
        props["index"] = int(index)
    existing = [t["title"] for t in _tabs(acct, spreadsheet_id)]
    if title in existing:
        # The API answers 400 with a generic message; say which titles are taken
        # so the caller can pick another one instead of retrying blind.
        raise ValueError(
            f"a tab named '{title}' already exists in this spreadsheet "
            f"(tabs: {', '.join(existing)})")
    res = _sheets(acct, "POST spreadsheets/{spreadsheetId}:batchUpdate",
                  params={"spreadsheetId": spreadsheet_id},
                  body={"requests": [{"addSheet": {"properties": props}}]})
    added = ((res.get("replies") or [{}])[0].get("addSheet", {}) or {}).get("properties", {})
    return {"account": acct, "spreadsheet_id": spreadsheet_id,
            "title": added.get("title", title), "sheet_id": added.get("sheetId"),
            "index": added.get("index"), "kept_tabs": existing,
            "url": _URL.format(spreadsheet_id), "ok": True}


def append_rows(spreadsheet_id: str, tab: str, rows: list[list],
                account: Optional[str] = None) -> dict:
    """Appends rows AFTER the last populated one of `tab`. Overwrites nothing.

    The safe write, and the one most tasks actually need. Values go in as if
    typed by a user (USER_ENTERED), so `=SUM(...)` stays a formula and a date
    stays a date.
    """
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, spreadsheet_id, "gsheets")
    if not rows:
        raise ValueError("rows vuoto: niente da aggiungere")
    res = _sheets(acct, "POST spreadsheets/{spreadsheetId}/values/{range}:append",
                  params={"spreadsheetId": spreadsheet_id, "range": tab,
                          "valueInputOption": "USER_ENTERED",
                          "insertDataOption": "INSERT_ROWS"},
                  body={"values": rows})
    up = res.get("updates", {})
    return {"account": acct, "spreadsheet_id": spreadsheet_id,
            "range": up.get("updatedRange"), "appended_rows": up.get("updatedRows", len(rows)),
            "url": _URL.format(spreadsheet_id), "ok": True}


def write_range(spreadsheet_id: str, range: str,  # noqa: A002 - MCP arg name
                values: list[list], account: Optional[str] = None) -> dict:
    """Writes `values` into an explicit A1 `range`, OVERWRITING what is there.

    The only destructive verb of this module, hence the explicit range: there is
    no default and no whole-tab shorthand, because overwriting cells the caller
    did not name is how formulas disappear. For adding data use `append_rows`.
    """
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, spreadsheet_id, "gsheets")
    if not (range or "").strip():
        raise ValueError("range obbligatorio (es. 'Foglio1!A1:C10'): "
                         "write_range sovrascrive, quindi non ha un default")
    if not values:
        raise ValueError("values vuoto: niente da scrivere")
    res = _sheets(acct, "PUT spreadsheets/{spreadsheetId}/values/{range}",
                  params={"spreadsheetId": spreadsheet_id, "range": range,
                          "valueInputOption": "USER_ENTERED"},
                  body={"values": values})
    return {"account": acct, "spreadsheet_id": spreadsheet_id,
            "range": res.get("updatedRange", range),
            "updated_cells": res.get("updatedCells"),
            "url": _URL.format(spreadsheet_id), "ok": True}
