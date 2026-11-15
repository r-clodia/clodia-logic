"""One-shot: job imports of a legacy pack datastore follow it to its instance
(clodia-platform#685).

The gateway's `python -m server.migrate_legacy_datastores --apply` turns each
legacy pack datastore file into an instance (`datastore:<pack>/<name>` →
`datastore:<pack>/<name>:<instance>`) and rewrites the catalogue and every
topic/DM import. A job's imports live HERE, in the scheduler's records
(#670), so this script applies the same mapping to them. The mapping is the
one the gateway wrote to `CLODIA_DATA/datastores/legacy-migration.json`
(the datadir is shared), or the `--map OLD=NEW` given here.

Usage (in the agent-server container, after the gateway's migration):

    python -m server.scheduler.migrate_job_data             # dry run: the report only
    python -m server.scheduler.migrate_job_data --apply
    python -m server.scheduler.migrate_job_data --map datastore:a/b=datastore:a/b:x --apply

Idempotent: a job that no longer names an old key is left alone.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import data_path
from . import db

MAP_FILE = Path("datastores") / "legacy-migration.json"


def load_map(path: Path | None = None) -> dict[str, str]:
    """`{"datastore:<pack>/<name>": "datastore:<pack>/<name>:<instance>"}`."""
    p = path or data_path(str(MAP_FILE))
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    m = raw.get("map") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in (m or {}).items()
            if str(k).startswith("datastore:") and str(v).startswith("datastore:")}


def plan(mapping: dict[str, str]) -> list[dict]:
    """The jobs whose `data` names an old key, with their data before/after."""
    rows = []
    for job in db.list_jobs():
        before = db.clean_data(job.get("data"))
        after = []
        changed = False
        for e in before:
            key = f"{e['kind']}:{e['name']}"
            if key in mapping:
                kind, _, name = mapping[key].partition(":")
                after.append({"kind": kind, "name": name})
                changed = True
            else:
                after.append(e)
        if changed:
            rows.append({"job": job["id"], "name": job.get("name"),
                         "before": before, "after": db.clean_data(after)})
    return rows


def apply(rows: list[dict]) -> list[dict]:
    for row in rows:
        row["done"] = db.set_job_data(int(row["job"]), row["after"]) is not None
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.scheduler.migrate_job_data",
                                 description="Job imports of legacy pack datastores → their "
                                             "instances (clodia-platform#685). Dry run by default.")
    ap.add_argument("--apply", action="store_true", help="do it (default: report only)")
    ap.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                    help="datastore:<pack>/<name>=datastore:<pack>/<name>:<instance> (repeatable; "
                         "default: the gateway's legacy-migration.json)")
    args = ap.parse_args(argv)
    mapping = load_map()
    for item in args.map:
        k, sep, v = item.partition("=")
        if not sep or not k.startswith("datastore:") or not v.startswith("datastore:"):
            ap.error(f"--map: atteso datastore:OLD=datastore:NEW, non '{item}'")
        mapping[k] = v
    rows = plan(mapping)
    if args.apply:
        rows = apply(rows)
    print(json.dumps({"apply": bool(args.apply), "map": mapping, "jobs": rows},
                     indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
