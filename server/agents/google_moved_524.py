"""Test support for the Google suites moved from clodia-tools (#524).

The gateway's native Google client (`server/tools/gdrive.py`, `gdrive_root.py`,
`google_io.py`, …) was removed; google-pack's gateway component is the
implementation. These suites ran against the native modules; here they run,
unchanged in what they assert, against the component's modules, registered on
the fake host of `test_558_google_pack` under a package of their own.

What the native suites set through the gateway's globals is set here through
the host, which is where the component reads it:

* `gdrive_roots` and the egress list → `HOST.perimeter.roots` / `.egress`
  (`cfg`);
* the signed channel claim → `HOST.chan` (`channel`);
* a topic's declared Drive folder → `HOST.perimeter.topics`.

Not a test module (no `test_` prefix).
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
import tempfile
import types

from .test_558_google_pack import GATEWAY, FakeHost

PKG = "googlepack_moved_524"


def _load():
    for k in [k for k in sys.modules if k == PKG or k.startswith(PKG + ".")]:
        sys.modules.pop(k, None)
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(GATEWAY)]
    pkg.__spec__ = importlib.util.spec_from_loader(PKG, loader=None, is_package=True)
    pkg.__spec__.submodule_search_locations = [str(GATEWAY)]
    sys.modules[PKG] = pkg
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True        # the gateway refuses a tree with __pycache__
    try:
        comp = importlib.import_module(f"{PKG}.google_component")
        host = FakeHost(tempfile.mkdtemp(prefix="gp524-"))
        comp.register(host)
        mods = {n: importlib.import_module(f"{PKG}.{n}") for n in (
            "gdrive_root", "gdrive", "google_io", "gsheets", "gdocs", "gcalendar",
            "gdrive_labels", "routes", "verbs", "google_oauth", "_host")}
    finally:
        sys.dont_write_bytecode = before
    return host, mods


HOST, MODS = _load()
gr = MODS["gdrive_root"]
gdrive = MODS["gdrive"]
google_io = MODS["google_io"]
gsheets = MODS["gsheets"]
gdocs = MODS["gdocs"]
gcalendar = MODS["gcalendar"]
gdrive_labels = MODS["gdrive_labels"]
routes = MODS["routes"]
verbs = MODS["verbs"]
google_oauth = MODS["google_oauth"]


def rebind() -> None:
    """Point the component's host back at ours (another suite may have
    registered the component on a host of its own in between)."""
    MODS["_host"].HOST = HOST


@contextlib.contextmanager
def cfg(roots=None, egress=()):
    """The gateway config `gdrive_roots` and the egress list, as the host
    exposes them to the component."""
    rebind()
    per = HOST.perimeter
    old = (per.roots, per.egress)
    per.roots, per.egress = dict(roots or {}), list(egress)
    try:
        yield
    finally:
        per.roots, per.egress = old


@contextlib.contextmanager
def channel(ch):
    """The signed channel claim of the call (`<tier>/<topic>` or None)."""
    rebind()
    old = HOST.chan
    HOST.chan = ch
    try:
        yield
    finally:
        HOST.chan = old
