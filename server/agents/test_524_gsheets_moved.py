"""Tests for gsheets.* (clodia-platform#118).

A fake Sheets service, not the network: what matters here is that each verb is
INCREMENTAL — that it names only what it touches and sends nothing about the
rest of the spreadsheet. A test against the real API would prove the call works;
these prove the call cannot take the other tabs with it.

On google-pack's gateway component (moved from clodia-tools
`server/tools/test_gsheets.py` when the gateway's native Google client was
removed, clodia-platform#524).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from .google_fake_524 import ServiceTransport
from .google_moved_524 import gdrive, google_io, gsheets, rebind


class _Exec:
    def __init__(self, result, log=None, entry=None):
        self._r, self._log, self._entry = result, log, entry

    def execute(self):
        if self._log is not None and self._entry is not None:
            self._log.append(self._entry)
        return self._r


class _Values:
    def __init__(self, outer):
        self.o = outer

    def get(self, spreadsheetId, range, valueRenderOption):  # noqa: N803
        self.o.calls.append(("values.get", spreadsheetId, range, valueRenderOption))
        return _Exec({"range": range, "values": self.o.values})

    def append(self, spreadsheetId, range, valueInputOption,  # noqa: N803
               insertDataOption, body):
        self.o.calls.append(("values.append", range, valueInputOption,
                             insertDataOption, body["values"]))
        return _Exec({"updates": {"updatedRange": f"{range}!A9",
                                  "updatedRows": len(body["values"])}})

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        self.o.calls.append(("values.update", range, valueInputOption, body["values"]))
        return _Exec({"updatedRange": range,
                      "updatedCells": sum(len(r) for r in body["values"])})


class _Spreadsheets:
    def __init__(self, outer):
        self.o = outer

    def get(self, spreadsheetId, fields):  # noqa: N803
        self.o.calls.append(("get", fields))
        return _Exec({"properties": {"title": "budget"},
                      "sheets": [{"properties": {"title": t, "sheetId": 100 + i,
                                                 "index": i,
                                                 "gridProperties": {"rowCount": 50,
                                                                    "columnCount": 10}}}
                                 for i, t in enumerate(self.o.tabs)]})

    def batchUpdate(self, spreadsheetId, body):  # noqa: N802,N803
        self.o.calls.append(("batchUpdate", body))
        props = body["requests"][0]["addSheet"]["properties"]
        return _Exec({"replies": [{"addSheet": {"properties": {
            "title": props["title"], "sheetId": 999,
            "index": props.get("index", len(self.o.tabs))}}}]})

    def values(self):
        return _Values(self.o)


class _Svc:
    def __init__(self, tabs, values=None):
        self.tabs, self.values, self.calls = tabs, values or [], []

    def spreadsheets(self):
        return _Spreadsheets(self)


class GSheetsTest(unittest.TestCase):
    def setUp(self):
        self.svc = _Svc(["preventivo", "personale", "rimodulato"],
                        values=[["a", "b"], ["1", "2"]])
        # Since #524 the calls go through `google_io`; the fake keeps the
        # discovery-client shape and `ServiceTransport` maps one onto the other.
        rebind()
        for p in (patch.object(google_io, "_transport", ServiceTransport(self.svc)),
                  patch.object(gdrive, "gworkspace_accounts", lambda: ["devnullboxx"])):
            p.start()
            self.addCleanup(p.stop)

    def test_list_tabs_reports_titles_and_sizes(self):
        r = gsheets.list_tabs("SID")
        self.assertEqual([t["title"] for t in r["tabs"]],
                         ["preventivo", "personale", "rimodulato"])
        self.assertEqual(r["tabs"][0]["rows"], 50)
        self.assertEqual(r["account"], "devnullboxx")

    def test_add_tab_does_not_touch_the_other_tabs(self):
        """The point of #118: the request must carry ONLY the new tab.

        The download/re-upload path failed because it rewrote the whole file.
        Here the batchUpdate body must mention the new title and nothing about
        the existing tabs — no values, no properties, no ids.
        """
        r = gsheets.add_tab("SID", "consuntivo")
        body = [c for c in self.svc.calls if c[0] == "batchUpdate"][0][1]
        self.assertEqual(body, {"requests": [{"addSheet": {
            "properties": {"title": "consuntivo"}}}]})
        self.assertEqual(r["sheet_id"], 999)
        self.assertEqual(r["kept_tabs"], ["preventivo", "personale", "rimodulato"])

    def test_add_tab_refuses_a_duplicate_title_with_the_taken_names(self):
        with self.assertRaises(ValueError) as cm:
            gsheets.add_tab("SID", "personale")
        msg = str(cm.exception)
        self.assertIn("personale", msg)
        self.assertIn("preventivo", msg)   # tells which titles are taken
        self.assertFalse([c for c in self.svc.calls if c[0] == "batchUpdate"])

    def test_read_defaults_to_the_first_tab(self):
        r = gsheets.read("SID")
        self.assertEqual(r["rows"], 2)
        self.assertIn(("values.get", "SID", "preventivo", "FORMATTED_VALUE"), self.svc.calls)

    def test_read_honours_an_explicit_range(self):
        gsheets.read("SID", range="personale!A1:C5")
        self.assertIn(("values.get", "SID", "personale!A1:C5", "FORMATTED_VALUE"),
                      self.svc.calls)

    def test_read_can_return_formula_text_instead_of_the_computed_value(self):
        """A default read is lossy where a formula lives, and silently so.

        Found in the live end-to-end check: `=SUM(B1:C1)` came back as `0`. An
        agent reading a budget to reproduce it would write back constants,
        destroying what this module exists to preserve.
        """
        r = gsheets.read("SID", tab="preventivo", formulas=True)
        self.assertIn(("values.get", "SID", "preventivo", "FORMULA"), self.svc.calls)
        self.assertTrue(r["formulas"])

    def test_append_rows_inserts_rows_and_keeps_formulas(self):
        r = gsheets.append_rows("SID", "personale", [["x", "=SUM(A1:A2)"]])
        call = [c for c in self.svc.calls if c[0] == "values.append"][0]
        self.assertEqual(call[1], "personale")
        self.assertEqual(call[2], "USER_ENTERED")   # '=SUM' stays a formula
        self.assertEqual(call[3], "INSERT_ROWS")    # never overwrites
        self.assertEqual(r["appended_rows"], 1)

    def test_write_range_requires_an_explicit_range(self):
        """It is the only destructive verb, so it must not have a default."""
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                gsheets.write_range("SID", bad, [["x"]])
        self.assertFalse([c for c in self.svc.calls if c[0] == "values.update"])

    def test_write_range_writes_where_told(self):
        r = gsheets.write_range("SID", "preventivo!B2:C3", [["1", "2"], ["3", "4"]])
        call = [c for c in self.svc.calls if c[0] == "values.update"][0]
        self.assertEqual(call[1], "preventivo!B2:C3")
        self.assertEqual(r["updated_cells"], 4)

    def test_empty_payloads_are_refused_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            gsheets.append_rows("SID", "personale", [])
        with self.assertRaises(ValueError):
            gsheets.write_range("SID", "personale!A1", [])


if __name__ == "__main__":
    unittest.main()
