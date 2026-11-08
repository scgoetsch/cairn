#!/usr/bin/env python3
"""Check a `cairn history-from-dolt` file against the Dolt store it came from.

    dev/verify-dolt-history.py BEADS_DIR HISTORY.jsonl EXPORT.jsonl [--dolt PATH]

EXPORT is the bd export the migration used (`bd export --all`, issues and memories). Passes when
every memory body and every issue text value (title, description, design, acceptance criteria,
notes, close reason) that Dolt ever held can be recovered from HISTORY plus EXPORT: a memory body
as a replaced `old_value` or as the live body, an issue text by walking the issue's field changes
back from the exported value (an `appended` tail peels off the end). Prints the counts; exit 1
with the first missing values otherwise. Reads a COPY of the Dolt store, as the verb does.
A deleted issue's chain starts at its `deleted` payload. Not handled: an id deleted, issued again
and deleted again (bd never reissues an id): its two lives are walked as one.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairn import dolthistory  # noqa: E402 — after the path insert, on purpose

TEXT = ("title", "description", "design", "acceptance_criteria", "notes", "close_reason")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source")
    ap.add_argument("history")
    ap.add_argument("export")
    ap.add_argument("--dolt", default="dolt")
    args = ap.parse_args()

    with dolthistory.dolt_copy(Path(args.source), args.dolt) as (_, query):
        like = dolthistory.MEMORY_PREFIX + "%"
        mem_rows = query("SELECT DISTINCT `key` AS k, `value` AS v FROM dolt_history_config "  # noqa: S608 — the module constant MEMORY_PREFIX only
                         f"WHERE `key` LIKE '{like}'")
        cols = ", ".join(TEXT)
        issue_rows = query(f"SELECT DISTINCT id, {cols} FROM dolt_history_issues")  # noqa: S608 — constant column names

    events = [json.loads(line) for line in Path(args.history).read_text().splitlines() if line]
    export = [json.loads(line) for line in Path(args.export).read_text().splitlines() if line]

    want_mem = {(r["k"][len(dolthistory.MEMORY_PREFIX):], r["v"]) for r in mem_rows}
    have_mem = {(r["key"], r["value"]) for r in export if r.get("_type") == "memory"}
    have_mem |= {(e["memory_key"], e["data"]["old_value"]) for e in events
                 if e["kind"] in ("memory_set", "memory_deleted")
                 and e["data"].get("old_value") is not None}

    want_text = {(r["id"], c, r[c]) for r in issue_rows for c in TEXT if r.get(c) not in (None, "")}
    current = {(r["id"], c): r[c] for r in export if r.get("_type", "issue") == "issue"
               for c in TEXT if r.get(c) not in (None, "")}
    # A deleted issue is not in the export: its last state is the payload of its `deleted` event.
    exported_ids = {r["id"] for r in export if r.get("_type", "issue") == "issue"}
    for e in events:
        if e["kind"] == "deleted" and e["issue_id"] not in exported_ids:
            issue = e["data"].get("issue") or {}
            for c in TEXT:
                if issue.get(c) not in (None, ""):
                    current[(e["issue_id"], c)] = issue[c]
    changes = defaultdict(list)
    for e in events:
        if e["kind"] == "field_change" and e["data"].get("field") in TEXT:
            changes[(e["issue_id"], e["data"]["field"])].append(e["data"])
    have_text = {(e["issue_id"], "title", e["data"]["title"]) for e in events
                 if e["kind"] == "created" and e["data"].get("title")}
    for key in set(changes) | set(current):
        text = current.get(key)
        if text is not None:
            have_text.add((*key, text))
        for data in reversed(changes.get(key, [])):
            if "appended" in data:
                if text is None or not text.endswith(data["appended"]):
                    print(f"broken append chain: {key}", file=sys.stderr)
                    return 1
                text = text[:-len(data["appended"])]
            else:
                text = data.get("old_value")
            if text not in (None, ""):
                have_text.add((*key, text))

    lost_mem = sorted(want_mem - have_mem)
    lost_text = sorted(want_text - have_text)
    print(f"memory versions in Dolt: {len(want_mem)}, recoverable: {len(want_mem) - len(lost_mem)}")
    print(f"issue text values in Dolt: {len(want_text)}, "
          f"recoverable: {len(want_text) - len(lost_text)}")
    for key, value in lost_mem[:5]:
        print(f"  missing memory version: {key} ({len(value)} chars)")
    for issue_id, col, value in lost_text[:5]:
        print(f"  missing {col} of {issue_id} ({len(value)} chars)")
    return 1 if lost_mem or lost_text else 0


if __name__ == "__main__":
    sys.exit(main())
