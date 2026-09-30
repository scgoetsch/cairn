"""Command line. Verbs and flags follow bd's, so a hook written against bd ports
by changing the program name. The current stage is read-only: write verbs are
refused.

Exit codes: 0 ok; 1 not found, differences, or `check` errors; 2 refused, bad
arguments, or could not run. A crash is reported as 2 — never as 1, which a
caller would read as "differences found".
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import PROG, __version__, prime, shadow, timeutil
from .importer import SourceError, build_store
from .queries import LINKED_OMITS, Snapshot, check, memories, recall, stats
from .store import (GITIGNORE, STORE_DIRNAME, StoreError, db_path, find_store_dir,
                    load_config, open_db)

GLYPH = {"open": "○", "in_progress": "◐", "blocked": "●", "closed": "✓", "deferred": "❄"}
LEGEND = "Status: ○ open  ◐ in_progress  ● blocked  ✓ closed  ❄ deferred"
RULE = "-" * 80
WRITE_VERBS = ("create", "update", "close", "reopen", "delete", "dep", "remember",
               "forget", "comment", "note", "defer", "undefer", "claim", "supersede",
               "duplicate", "init")
PREVIEW = 120


class Refused(Exception):
    """A request cairn will not carry out (exit 2)."""


def emit_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def parse_priority(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    digits = text[1:] if text[:1] in ("P", "p") else text
    if not digits.isdigit() or not 0 <= int(digits) <= 4:
        raise Refused(f"invalid priority '{text}' (expected 0-4 or P0-P4)")
    return int(digits)


def limited(ids: List[str], limit: int, *, json_out: bool = False) -> List[str]:
    if limit < 0:
        raise Refused(f"invalid limit {limit} (use 0 for unlimited)")
    if limit == 0 or len(ids) <= limit:
        return ids
    if json_out:
        # bd truncates at the same default without a word, and a capped listing
        # reads as a complete one. The JSON array keeps bd's shape; the notice
        # goes to stderr where it cannot break a parser.
        print(f"{PROG}: showing {limit} of {len(ids)} issues (use --limit 0 for all)",
              file=sys.stderr)
    return ids[:limit]


def issue_line(snap: Snapshot, issue_id: str, *, with_parent: bool = False) -> str:
    row = snap.issues[issue_id]
    kind = "" if row["issue_type"] == "task" else f"[{row['issue_type']}] "
    line = f"{GLYPH[row['status']]} {issue_id} ● P{row['priority']} {kind}{row['title']}"
    if with_parent:
        parents = [p for p in snap.parents_of.get(issue_id, []) if p in snap.issues]
        if parents:
            line += f" ← {snap.issues[parents[0]]['title']}"
    return line


def store_dir_or_fail(start: Path) -> Path:
    found = find_store_dir(start)
    if found is None:
        raise StoreError(f"no {STORE_DIRNAME}/ store found from {start} upwards "
                         f"(set CAIRN_DIR, or run `{PROG} shadow sync` next to a .beads/ store)")
    return found


# Connections opened while serving one command; main() closes them all, so an
# in-process caller (the tests, or an embedding tool) does not leak handles.
_OPEN: List[sqlite3.Connection] = []


def connect(start: Path) -> sqlite3.Connection:
    conn = open_db(db_path(store_dir_or_fail(start)))
    _OPEN.append(conn)
    return conn


def ensure_store_dir(path: Path) -> None:
    """Create the store directory. Git ignores everything in it except its
    config.json: the database and the log are derived and rebuilt on each sync."""
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GITIGNORE, encoding="utf-8")


# -- verbs ----------------------------------------------------------------

def cmd_where(args, start: Path) -> int:
    store = store_dir_or_fail(start)
    if args.json:
        emit_json({"store": str(store), "database": str(db_path(store))})
    else:
        print(store)
        print(f"  database: {db_path(store)}")
    return 0


def cmd_stats(args, start: Path) -> int:
    conn = connect(start)
    data = stats(conn)
    if args.json:
        emit_json(data)
        return 0
    print(f"Issues: {data['issues']}  (" + ", ".join(
        f"{n} {s}" for s, n in data["by_status"].items()) + f"; {data['blocked']} blocked)")
    print(f"Dependencies: {data['dependencies']}  Comments: {data['comments']}  "
          f"Events: {data['events']}")
    print(f"Memories: {data['memories']}  Memory links: {data['memory_edges']}")
    return 0


def cmd_ready(args, start: Path) -> int:
    snap = Snapshot(connect(start))
    ids = snap.ready(now=timeutil.now_iso(), include_deferred=args.include_deferred,
                     priority=parse_priority(args.priority), issue_type=args.type,
                     parent=args.parent, assignee=args.assignee, unassigned=args.unassigned)
    total = len(ids)
    ids = limited(ids, args.limit, json_out=args.json)
    if args.json:
        emit_json([snap.issue_object(i) for i in ids])
        return 0
    for issue_id in ids:
        print(issue_line(snap, issue_id, with_parent=True))
    print()
    print(RULE)
    shown = "" if len(ids) == total else f" (showing {len(ids)})"
    print(f"Ready: {total} issues with no active blockers{shown}")
    print()
    print(LEGEND)
    return 0


def cmd_list(args, start: Path) -> int:
    snap = Snapshot(connect(start))
    statuses = [s.strip() for s in args.status.split(",")] if args.status else None
    for status in statuses or []:
        if status not in GLYPH:
            raise Refused(f"unknown status '{status}' (expected one of {', '.join(GLYPH)})")
    ids = snap.select(statuses=statuses, include_closed=args.all, parent=args.parent,
                      issue_type=args.type, priority=parse_priority(args.priority),
                      labels=args.label or [])
    total = len(ids)
    ids = limited(ids, args.limit, json_out=args.json)
    if args.json:
        emit_json([snap.issue_object(i) for i in ids])
        return 0
    if not ids:
        print("No issues found.")
        return 0
    for issue_id in ids:
        print(issue_line(snap, issue_id))
    counts: Dict[str, int] = {}
    for issue_id in ids:
        status = snap.issues[issue_id]["status"]
        counts[status] = counts.get(status, 0) + 1
    print()
    print(RULE)
    shown = "" if len(ids) == total else f" of {total}"
    print(f"Total: {len(ids)}{shown} issues (" + ", ".join(
        f"{counts.get(s, 0)} {s.replace('_', ' ')}" for s in ("open", "in_progress")) + ")")
    print()
    print(LEGEND)
    return 0


def cmd_blocked(args, start: Path) -> int:
    snap = Snapshot(connect(start))
    blocked = snap.blocked()
    ids = snap.ordered(blocked)
    if args.json:
        out = []
        for issue_id in ids:
            # bd's `blocked` prints a narrower object than `list` does.
            obj = {k: v for k, v in snap.issue_object(issue_id).items()
                   if k not in LINKED_OMITS}
            obj["blocked_by"] = blocked[issue_id]
            obj["blocked_by_count"] = len(blocked[issue_id])
            out.append(obj)
        emit_json(out)
        return 0
    for issue_id in ids:
        print(issue_line(snap, issue_id))
        print(f"    blocked by: {', '.join(blocked[issue_id])}")
    print()
    print(f"Blocked: {len(ids)} issues")
    return 0


def cmd_show(args, start: Path) -> int:
    snap = Snapshot(connect(start))
    if args.id not in snap.issues:
        print(f'Error fetching {args.id}: no issue found matching "{args.id}"', file=sys.stderr)
        return 1
    obj = snap.issue_object(args.id, shape="show")
    if args.json:
        emit_json([obj])
        return 0
    row = snap.issues[args.id]
    print(f"{GLYPH[row['status']]} {args.id} · {row['title']}   "
          f"[P{row['priority']} · {row['status'].upper()}]")
    people = [f"Owner: {row['owner']}" if row["owner"] else "",
              f"Assignee: {row['assignee']}" if row["assignee"] else "",
              f"Type: {row['issue_type']}"]
    print(" · ".join(p for p in people if p))
    dates = [f"Created: {row['created_at'][:10]}", f"Updated: {row['updated_at'][:10]}"]
    if row["defer_until"]:
        dates.append(f"Deferred until: {row['defer_until'][:10]}")
    print(" · ".join(dates))
    if row["close_reason"]:
        print(f"Close reason: {row['close_reason']}")
    for heading, col in (("DESCRIPTION", "description"), ("DESIGN", "design"),
                         ("NOTES", "notes"), ("ACCEPTANCE CRITERIA", "acceptance_criteria")):
        if row[col]:
            print(f"\n{heading}\n")
            for line in row[col].splitlines():
                print(f"  {line}")
    blocked_by = snap.blocked().get(args.id)
    if blocked_by:
        print(f"\nBLOCKED BY\n  {', '.join(blocked_by)}")
    for heading, key in (("DEPENDS ON", "dependencies"), ("DEPENDENTS", "dependents")):
        if obj.get(key):
            print(f"\n{heading}")
            for linked in obj[key]:
                title = linked.get("title", "(missing)")
                print(f"  {linked['dependency_type']}: {linked['id']} {title}")
    for comment in obj.get("comments", []):
        print(f"\nCOMMENT {comment['created_at'][:10]} {comment['author'] or ''}\n")
        for line in comment["text"].splitlines():
            print(f"  {line}")
    return 0


def cmd_memories(args, start: Path) -> int:
    conn = connect(start)
    rows = memories(conn, args.keyword)
    if args.json:
        out: Dict[str, Any] = {r["key"]: r["value"] for r in rows}
        out["schema_version"] = 1          # bd includes this key; consumers strip it
        emit_json(out)
        return 0
    if args.keyword:
        print(f'Memories matching "{args.keyword}":')
    else:
        print(f"Memories ({len(rows)}):")
    print()
    for row in rows:
        body = " ".join(row["value"].split())
        print(f"  {row['key']}")
        print(f"    {body[:PREVIEW]}{'...' if len(body) > PREVIEW else ''}")
        print()
    if not rows:
        print("  (none)")
    return 0


def cmd_recall(args, start: Path) -> int:
    conn = connect(start)
    row = recall(conn, args.key)
    if args.json:
        emit_json({"found": row is not None, "key": args.key, "schema_version": 1,
                   **({"value": row["value"]} if row is not None else {})})
        return 0 if row is not None else 1
    if row is None:
        print(f'No memory with key "{args.key}"')
        return 1
    print(row["value"])
    return 0


def cmd_prime(args, start: Path) -> int:
    conn = connect(start)
    sys.stdout.write(prime.build(conn, now=timeutil.now_iso(), budget=args.budget))
    return 0


def cmd_export(args, start: Path) -> int:
    conn = connect(start)
    snap = Snapshot(conn)
    lines = [json.dumps(snap.issue_object(i, shape="export"), ensure_ascii=False,
                        separators=(",", ":")) for i in sorted(snap.issues)]
    issue_count = len(lines)
    if args.include_memories or args.all:
        lines += [json.dumps({"_type": "memory", "key": r["key"], "value": r["value"]},
                             ensure_ascii=False, separators=(",", ":"))
                  for r in memories(conn)]
    text = "".join(line + "\n" for line in lines)
    if not args.output:
        sys.stdout.write(text)
        return 0
    target = Path(args.output)
    temp = target.with_name(f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    print(f"Exported {issue_count} issues to {args.output}")
    return 0


def cmd_check(args, start: Path) -> int:
    conn = connect(start)
    result = check(conn)
    if args.json:
        emit_json(result)
        return 1 if result["errors"] else 0
    for message in result["errors"]:
        print(f"✗ {message}")
    for message in result["warnings"]:
        print(f"⚠ {message}")
    for message in result["notes"]:
        print(f"ℹ {message}")
    counts = stats(conn)
    verdict = "✗ FAIL" if result["errors"] else "✓ OK"
    print(f"{verdict} — {counts['issues']} issues, {counts['dependencies']} dependencies, "
          f"{counts['memories']} memories, {counts['memory_edges']} memory links, "
          f"{len(result['errors'])} errors, {len(result['warnings'])} warnings")
    return 1 if result["errors"] else 0


def cmd_import(args, start: Path) -> int:
    for option in (args.file, args.interactions, args.ledger, args.hot):
        if option and not Path(option).is_file():
            raise Refused(f"no such file: {option}")
    store = find_store_dir(start) or start / STORE_DIRNAME
    ensure_store_dir(store)
    report = build_store(
        db_path(store), Path(args.file),
        interactions_path=Path(args.interactions) if args.interactions else None,
        ledger_path=Path(args.ledger) if args.ledger else None,
        hot_path=Path(args.hot) if args.hot else None,
        extra_prefixes=load_config(store).get("issue_prefixes", []))
    if args.json:
        emit_json({"counts": report.counts, "losses": report.losses,
                   "warnings": report.warnings})
    else:
        print("Imported " + ", ".join(f"{n} {k}" for k, n in report.counts.items()))
        for loss in report.losses:
            print(f"✗ {loss}")
        for warning in report.warnings:
            print(f"⚠ {warning}")
    return 1 if report.losses else 0


def cmd_shadow(args, start: Path) -> int:
    if args.action == "status":
        store = find_store_dir(start) or shadow.find_workspace(start) / STORE_DIRNAME
        data = shadow.status(store)
        if args.json:
            emit_json(data)
            return 0
        print(f"Shadow runs: {data['runs']} ({data['failed_runs']} not clean, "
              f"{data['could_not_run']} could not run)")
        print(f"Current clean streak: {data['clean_streak']} runs over "
              f"{data['clean_streak_days']} days (since {data['clean_streak_since']})")
        print(f"Exit criteria ({data['criteria']}): "
              f"{'MET' if data['criteria_met'] else 'not yet'}")
        if data["last_failure"]:
            print(f"Last not-clean run: {data['last_failure']['at']} — "
                  f"{'; '.join(data['last_failure'].get('first', []))}")
        if data["last_error"]:
            print(f"Last run that could not complete: {data['last_error']['at']} — "
                  f"{data['last_error'].get('error')}")
        if data["unreadable_log_lines"]:
            print(f"Unreadable log lines skipped: {data['unreadable_log_lines']}")
        return 0

    # bd runs where the .beads store is; the cairn store may live elsewhere (CAIRN_DIR).
    workspace = shadow.find_workspace(start)
    store = find_store_dir(start) or workspace / STORE_DIRNAME
    ensure_store_dir(store)
    losses: List[str] = []
    warnings: List[str] = []
    differences: List[str] = []
    retried = False
    if args.action == "run":
        result = shadow.run(store, workspace, args.bd,
                            args.deadline or shadow.DEFAULT_DEADLINE_SECONDS)
        differences, retried = result.differences, result.retried
        losses, warnings = result.report.losses, result.report.warnings
    else:
        deadline = shadow.Deadline(args.deadline or shadow.MANUAL_DEADLINE_SECONDS)
        if args.action == "sync":
            report, differences = shadow.sync(store, workspace, args.bd, deadline)
            losses, warnings = report.losses, report.warnings
            if not args.quiet and not args.json:
                print("Synced " + ", ".join(f"{n} {k}" for k, n in report.counts.items()))
        else:
            differences = shadow.diff(store, workspace, args.bd, deadline=deadline)

    if args.json:
        emit_json({"differences": differences, "losses": losses, "warnings": warnings,
                   "retried": retried})
    else:
        for message in differences:
            print(f"DIFF {message}")
        for message in losses:
            print(f"LOSS {message}")
        if not args.quiet:
            # Oddities in bd's own data: worth seeing, but not a disagreement.
            for message in warnings:
                print(f"WARN {message}")
            if args.action != "sync" and not differences and not losses:
                note = " after a retry (bd changed during the first attempt)" if retried else ""
                print(f"✓ cairn agrees with bd ({args.action}){note}")
    return 1 if differences or losses else 0


# -- argument parsing -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Agent-facing work and memory store (read-only shadow of bd).")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("-C", "--directory", help="run as if started in this directory")
    parser.add_argument("--json", action="store_true", help="print JSON")
    # bd accepts --json before or after the verb. SUPPRESS keeps a verb-level
    # parser from resetting a --json given before the verb.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print JSON")
    verbs = parser.add_subparsers(dest="verb", metavar="<verb>")

    class _Sub:
        @staticmethod
        def add_parser(name: str, **kwargs: Any) -> argparse.ArgumentParser:
            return verbs.add_parser(name, parents=[common], **kwargs)

    sub = _Sub()

    sub.add_parser("where", help="show the resolved store").set_defaults(func=cmd_where)
    sub.add_parser("stats", help="store statistics").set_defaults(func=cmd_stats)

    p = sub.add_parser("ready", help="issues ready to work (open, not blocked, not deferred)")
    p.add_argument("-n", "--limit", type=int, default=100)
    p.add_argument("-p", "--priority")
    p.add_argument("-t", "--type")
    p.add_argument("--parent", help="only descendants of this issue")
    p.add_argument("-a", "--assignee")
    p.add_argument("-u", "--unassigned", action="store_true")
    p.add_argument("--include-deferred", action="store_true")
    p.set_defaults(func=cmd_ready)

    p = sub.add_parser("list", help="list issues (closed hidden unless --all or --status)")
    p.add_argument("-s", "--status", help="comma-separated stored statuses")
    p.add_argument("--all", action="store_true", help="include closed issues")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--parent", help="only direct children of this issue")
    p.add_argument("-t", "--type")
    p.add_argument("-p", "--priority")
    p.add_argument("-l", "--label", action="append")
    p.set_defaults(func=cmd_list)

    sub.add_parser("blocked", help="blocked issues and what blocks them").set_defaults(
        func=cmd_blocked)

    p = sub.add_parser("show", help="one issue in full")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("memories", help="list or search memories")
    p.add_argument("keyword", nargs="?")
    p.set_defaults(func=cmd_memories)

    p = sub.add_parser("recall", help="print one memory by key")
    p.add_argument("key")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("prime", help="session priming payload, held to a byte budget")
    p.add_argument("--budget", type=int, default=prime.DEFAULT_BUDGET)
    p.set_defaults(func=cmd_prime)

    p = sub.add_parser("export", help="JSONL export in bd's row format, sorted")
    p.add_argument("-o", "--output")
    p.add_argument("--include-memories", action="store_true")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_export)

    sub.add_parser("check", help="referential integrity of issues and memory links"
                   ).set_defaults(func=cmd_check)

    p = sub.add_parser("import", help="build the store from a bd JSONL export")
    p.add_argument("file")
    p.add_argument("--interactions", help="bd interactions.jsonl (becomes the event history)")
    p.add_argument("--ledger", help="bd-memgraph ledger (memory timestamps and deletions)")
    p.add_argument("--hot", help="hot list: one memory key per line")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("shadow", help="mirror bd and compare answers with it")
    p.add_argument("action", choices=("sync", "diff", "run", "status"))
    p.add_argument("--bd", default="bd", help="bd executable (default: bd)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print nothing when cairn and bd agree")
    p.add_argument("--deadline", type=float, metavar="SECONDS",
                   help=f"time budget for all bd calls (run: "
                        f"{shadow.DEFAULT_DEADLINE_SECONDS:g}s, sync/diff: "
                        f"{shadow.MANUAL_DEADLINE_SECONDS:g}s)")
    p.set_defaults(func=cmd_shadow)
    return parser


def _first_verb(argv: Sequence[str]) -> Optional[str]:
    skip_value = False
    for arg in argv:
        if skip_value:
            skip_value = False
        elif arg in ("-C", "--directory"):
            skip_value = True
        elif not arg.startswith("-"):
            return arg
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):      # glyphs must not crash an ASCII locale
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    verb = _first_verb(argv)
    if verb in WRITE_VERBS:
        print(f"{PROG}: '{verb}' refused — cairn is a read-only shadow at this stage; "
              "bd is the only writer. Use bd for this.", file=sys.stderr)
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    start = Path(args.directory) if args.directory else Path.cwd()

    # A supervisor's SIGTERM would otherwise end the process without running
    # `finally` blocks, leaving a temporary export and database behind.
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, terminate)
    except (ValueError, OSError):            # not the main thread
        pass
    try:
        return args.func(args, start)
    except (StoreError, SourceError, shadow.BdError, Refused) as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except (OSError, sqlite3.Error) as exc:
        print(f"{PROG}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:        # noqa: BLE001 — see the exit-code note at the top
        if os.environ.get("CAIRN_DEBUG"):
            raise
        print(f"{PROG}: internal error: {type(exc).__name__}: {exc} "
              "(set CAIRN_DEBUG=1 for a traceback)", file=sys.stderr)
        return 2
    finally:
        while _OPEN:
            _OPEN.pop().close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
