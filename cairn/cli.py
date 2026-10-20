"""Command line. Verbs and flags follow bd's, so a hook written against bd ports
by changing the program name. Write verbs work only on a store made by
`cairn init`; on a shadow of bd (`cairn shadow sync`) they are refused, because
bd is that store's only writer.

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
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import PROG, __version__, doltseed, prime, shadow, sync as syncmod, timeutil
from .importer import ENV_PREFIXES, SourceError, build_store, build_writable
from .queries import (LINKED_OMITS, Snapshot, check, events_lines, export_lines, memories,
                      recall, stats)
from .store import (GITIGNORE, STORE_DIRNAME, StoreError, db_path, find_store_dir,
                    get_meta, init_store, load_config, migrate, open_db, open_writable)
from .writer import WriteConflict, WriteRefused, Writer

GLYPH = {"open": "○", "in_progress": "◐", "blocked": "●", "closed": "✓", "deferred": "❄"}
LEGEND = "Status: ○ open  ◐ in_progress  ● blocked  ✓ closed  ❄ deferred"
RULE = "-" * 80
# bd verbs cairn does not implement; refused, never mistaken for something else.
UNSUPPORTED_VERBS = ("claim", "supersede", "duplicate", "edit")
PREVIEW = 120


class Refused(Exception):
    """A request cairn will not carry out (exit 2)."""


def refuse_write(what: str) -> int:
    print(f"{PROG}: '{what}' is not supported by cairn.", file=sys.stderr)
    return 2


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
    store = store_dir_or_fail(start)
    conn = open_db(db_path(store))
    _OPEN.append(conn)
    if get_meta(conn, "writable") == "1":
        state = syncmod.quick_state(conn, store)
        if state in (syncmod.BEHIND, syncmod.DIVERGED):
            print(f"{PROG}: warning: {syncmod.stale_message(state, store)}", file=sys.stderr)
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
    if args.claim:
        # bd's `ready --claim` claims the first ready issue: a write behind a read
        # verb, under the same compare-and-swap as `update --claim`.
        writer = open_writer(args, start, claim=True)
        claimed = writer.claim_ready(
            include_deferred=args.include_deferred, priority=parse_priority(args.priority),
            issue_type=args.type, parent=args.parent, assignee=args.assignee,
            unassigned=args.unassigned)
        if claimed is None:
            if args.json:
                emit_json([])
            else:
                print("No ready issue to claim.", file=sys.stderr)
            return 1
        return report_issues(args, writer, [claimed], "Claimed")
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


def allocator_seed(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The allocator state in the seed format `init --seed` reads: every id ever issued and,
    per parent, the last child number issued. It is what no JSONL export carries; tracked
    beside the exports, it lets a fresh checkout rebuild the store without reissuing an id."""
    used = [r[0] for r in conn.execute("SELECT id FROM used_ids ORDER BY id")]
    counters = {r[0]: r[1] - 1 for r in conn.execute(
        "SELECT parent_id, next_n FROM id_counters ORDER BY parent_id")}
    meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    return {"used_ids": used, "child_counters": counters,
            "bd_version": meta.get("bd_version"), "source": "cairn export --allocator"}


def cmd_export(args, start: Path) -> int:
    conn = connect(start)
    if args.events:
        if args.include_memories or args.all or args.allocator:
            raise Refused("--events writes the event history alone")
        lines = events_lines(conn)
        issue_count = 0
    elif args.allocator:
        if args.include_memories or args.all:
            raise Refused("--allocator writes the allocator state alone")
        if conn.execute("SELECT value FROM meta WHERE key = 'writable'").fetchone() is None:
            raise Refused("a shadow has no allocator state (bd allocates); --allocator needs "
                          "a writable store")
        seed = allocator_seed(conn)
        lines = [json.dumps(seed, indent=1, sort_keys=True)]
        issue_count = 0
    else:
        lines = export_lines(conn, bool(args.include_memories or args.all))
        issue_count = sum(1 for line in lines if line.startswith('{"_type":"issue"'))
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
    print(f"Exported {len(lines)} events to {args.output}" if args.events
          else f"Exported the allocator state to {args.output}" if args.allocator
          else f"Exported {issue_count} issues to {args.output}")
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


# -- write verbs ------------------------------------------------------------

def git_config(key: str, start: Path) -> Optional[str]:
    try:
        proc = subprocess.run(["git", "config", key], cwd=start, capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def resolve_actor(args, start: Path, *, claim: bool = False) -> str:
    """--actor, else BEADS_ACTOR, else (not for a claim) git user.name, else $USER.
    A claim needs a session's own name: bd's fallback to git user.name makes every
    session the same claimant, and the compare-and-swap then protects nothing."""
    actor = getattr(args, "actor", None) or os.environ.get("BEADS_ACTOR")
    if actor:
        return actor
    if claim:
        raise Refused("a claim needs an actor: set BEADS_ACTOR or pass --actor")
    return git_config("user.name", start) or os.environ.get("USER") or ""


def open_writer(args, start: Path, *, claim: bool = False) -> Writer:
    store = store_dir_or_fail(start)
    conn = open_writable(db_path(store))
    _OPEN.append(conn)
    state = syncmod.quick_state(conn, store)
    if state in (syncmod.BEHIND, syncmod.DIVERGED):
        raise Refused(f"write refused: {syncmod.stale_message(state, store)}")
    prefixes = [*load_config(store).get("issue_prefixes", []),
                *(p.strip() for p in os.environ.get(ENV_PREFIXES, "").split(",") if p.strip())]
    return Writer(conn, actor=resolve_actor(args, start, claim=claim),
                  owner=git_config("user.email", start), issue_prefixes=prefixes,
                  db_file=db_path(store))


def report_issues(args, writer: Writer, ids: Sequence[str], verb: str, extra: str = "") -> int:
    snap = Snapshot(writer.conn)
    if args.json:
        emit_json([snap.issue_object(i) for i in ids if i in snap.issues])
        return 0
    if getattr(args, "quiet", False):
        return 0
    for issue_id in ids:
        title = snap.issues[issue_id]["title"] if issue_id in snap.issues else ""
        print(f"✓ {verb} {issue_id} — {title}{extra}")
    return 0


def record_prefix(store: Path, prefix: str) -> None:
    """Write the issue prefix into the tracked .cairn/config.json, so `cairn sync` in a fresh
    clone knows it without -p. Other keys are kept."""
    path = store / "config.json"
    data = load_config(store)
    if data.get("prefix") == prefix:
        return
    data["prefix"] = prefix
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_sync(args, start: Path) -> int:
    store = store_dir_or_fail(start) if find_store_dir(start) else start / STORE_DIRNAME
    if args.status:
        if not db_path(store).is_file():
            state, detail = syncmod.BEHIND, "no store database here yet"
        else:
            conn = connect_quiet(store)
            state, detail = syncmod.full_state(conn, store)
        if syncmod.unmerged(store):
            state, detail = syncmod.DIVERGED, "export files unmerged in git"
        if args.json:
            emit_json({"state": state, "detail": detail})
        else:
            print(f"{state}: {detail}")
        return {syncmod.BEHIND: 1, syncmod.DIVERGED: 2}.get(state, 0)
    try:
        result = syncmod.sync(store, hot_path=store.parent / ".claude" / "memory-hot.txt")
    except StoreError as exc:
        raise Refused(str(exc)) from exc
    if args.json:
        emit_json({"state": result.state, "detail": result.detail, "rebuilt": result.rebuilt,
                   "backup": str(result.backup) if result.backup else None})
    elif not args.quiet:
        if result.rebuilt:
            kept = f"; the old database is {result.backup}" if result.backup else ""
            print(f"Synced {store} from {syncmod.exports_dir(store)} ({result.detail}){kept}")
        else:
            print(f"Nothing to do: {result.state}, {result.detail}")
    return 0


def connect_quiet(store: Path) -> sqlite3.Connection:
    conn = open_db(db_path(store))
    _OPEN.append(conn)
    return conn


def cmd_init(args, start: Path) -> int:
    # CAIRN_DIR names the store directory for every verb, init included.
    store = Path(os.environ["CAIRN_DIR"]) if os.environ.get("CAIRN_DIR") else start / STORE_DIRNAME
    if db_path(store).exists():
        raise Refused(f"{db_path(store)} already exists")
    sources = (args.seed, args.interactions, args.events, args.ledger, args.hot)
    if not args.source and any(sources):
        raise Refused("--seed, --interactions, --events, --ledger and --hot need --from")
    for option in (args.source, *sources):
        if option and not Path(option).is_file():
            raise Refused(f"no such file: {option}")
    ensure_store_dir(store)
    record_prefix(store, args.prefix)
    if not args.source:
        init_store(db_path(store), args.prefix).close()
        if not args.quiet:
            print(f"Initialised a writable cairn store at {store} (prefix {args.prefix})")
        return 0
    report = build_writable(
        db_path(store), Path(args.source), args.prefix,
        seed_path=Path(args.seed) if args.seed else None, bd_version=args.bd_version,
        interactions_path=Path(args.interactions) if args.interactions else None,
        events_path=Path(args.events) if args.events else None,
        ledger_path=Path(args.ledger) if args.ledger else None,
        hot_path=Path(args.hot) if args.hot else None,
        extra_prefixes=load_config(store).get("issue_prefixes", []))
    conn = open_db(db_path(store))
    try:
        bd_version = conn.execute("SELECT value FROM meta WHERE key = 'bd_version'").fetchone()
    finally:
        conn.close()
    bd_version = bd_version[0] if bd_version else None
    layout = ("byte-identical to the source" if report.byte_identical else
              "equal in content to the source; layout differs (the tracked export will be "
              "rewritten once)")
    if args.json:
        emit_json({"store": str(store), "counts": report.counts, "warnings": report.warnings,
                   "byte_identical": report.byte_identical, "bd_version": bd_version})
    elif not args.quiet:
        print(f"Initialised a writable cairn store at {store} (prefix {args.prefix}) from "
              f"{args.source}: " + ", ".join(f"{n} {k}" for k, n in report.counts.items()))
        print(f"✓ Verified: its export is {layout}. Source bd version: "
              f"{bd_version or 'unknown (pass --seed or --bd-version)'}")
        for warning in report.warnings:
            print(f"⚠ {warning}")
    return 0


def cmd_migrate(args, start: Path) -> int:
    store = find_store_dir(start)
    if store is None:
        raise Refused(f"no {STORE_DIRNAME}/ store found from {start} upwards")
    before, after, backup = migrate(db_path(store))
    if args.json:
        emit_json({"from": before, "to": after, "backup": str(backup) if backup else None})
    elif backup is None:
        print(f"{db_path(store)} is at schema version {after}; nothing to do")
    else:
        print(f"Migrated {db_path(store)} from schema version {before} to {after} "
              f"(backup: {backup})")
    return 0


def cmd_seed_from_dolt(args, start: Path) -> int:
    try:
        seed = doltseed.read_seed(Path(args.source), args.dolt)
    except doltseed.DoltError as exc:
        raise Refused(str(exc)) from exc
    text = json.dumps(seed, indent=1, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Seed written to {args.output}: bd {seed['bd_version'] or 'version unknown'}, "
              f"{len(seed['used_ids'])} ids ever issued ({len(seed['deleted_ids'])} deleted), "
              f"{len(seed['child_counters'])} child counters", file=sys.stderr)
        for note in seed["notes"]:
            print(f"⚠ {note}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def read_text_arg(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise Refused(f"cannot read {path}: {exc.strerror}") from exc


def body_arg(args) -> Optional[str]:
    body = read_text_arg(getattr(args, "body_file", None))
    if body is not None and args.description is not None:
        raise Refused("--description and --body-file both given")
    return body if body is not None else args.description


def labels_arg(values: Optional[List[str]]) -> List[str]:
    return [x.strip() for v in values or [] for x in v.split(",") if x.strip()]


def cmd_create(args, start: Path) -> int:
    title = args.title or args.title_pos
    if args.title and args.title_pos and args.title != args.title_pos:
        raise Refused("title given twice")
    writer = open_writer(args, start)
    new_id = writer.create(
        title or "", description=body_arg(args), design=args.design,
        acceptance=args.acceptance, notes=args.notes,
        priority=2 if args.priority is None else parse_priority(args.priority),
        issue_type=args.type or "task", parent=args.parent, issue_id=args.id,
        labels=labels_arg(args.labels), assignee=args.assignee, defer=args.defer)
    if args.silent:
        print(new_id)
        return 0
    if args.json:
        emit_json(Snapshot(writer.conn).issue_object(new_id))
        return 0
    if not args.quiet:
        print(f"✓ Created issue: {new_id} — {title}")
    return 0


def cmd_update(args, start: Path) -> int:
    writer = open_writer(args, start, claim=args.claim)
    for issue_id in args.ids:
        writer.update(issue_id, claim=args.claim, title=args.title, description=body_arg(args),
                      design=args.design, acceptance=args.acceptance, notes=args.notes,
                      append_notes=args.append_notes,
                      priority=None if args.priority is None else parse_priority(args.priority),
                      issue_type=args.type, status=args.status, assignee=args.assignee,
                      add_labels=labels_arg(args.add_label),
                      remove_labels=labels_arg(args.remove_label), defer=args.defer)
    return report_issues(args, writer, args.ids, "Updated issue:")


def cmd_close(args, start: Path) -> int:
    writer = open_writer(args, start)
    before = set(Snapshot(writer.conn).ready(now=timeutil.now_iso())) if args.suggest_next else set()
    closed = [i for i in args.ids if writer.close(i, args.reason, force=args.force)]
    for issue_id in args.ids:
        if issue_id not in closed and not args.json:
            print(f"{issue_id} was already closed; unchanged")
    code = report_issues(args, writer, closed, "Closed", f": {args.reason or 'Closed'}")
    if args.suggest_next and not args.json:
        snap = Snapshot(writer.conn)
        newly = [i for i in snap.ready(now=timeutil.now_iso()) if i not in before]
        for issue_id in newly:
            print(f"  now ready: {issue_line(snap, issue_id)}")
    return code


def cmd_reopen(args, start: Path) -> int:
    writer = open_writer(args, start)
    for issue_id in args.ids:
        writer.reopen(issue_id)
    return report_issues(args, writer, args.ids, "Reopened")


def cmd_note(args, start: Path) -> int:
    text = read_text_arg(args.file) if args.file else args.text
    if not text or (args.file and args.text):
        raise Refused("note needs exactly one of <text> or --file")
    writer = open_writer(args, start)
    writer.note(args.id, text.rstrip("\n"))
    return report_issues(args, writer, [args.id], "Note added to")


def cmd_comment(args, start: Path) -> int:
    writer = open_writer(args, start)
    writer.comment(args.id, args.text)
    return report_issues(args, writer, [args.id], "Comment added to")


def cmd_comments(args, start: Path) -> int:
    """bd's two shapes: `comments <id>` lists, `comments add <id> <text>` writes."""
    words = args.words
    if words[0] == "add":
        if len(words) != 3:
            raise Refused("usage: comments add <id> <text>")
        args.id, args.text, args.verb = words[1], words[2], "comment"
        return cmd_comment(args, start)
    if len(words) != 1:
        raise Refused("usage: comments <id>  |  comments add <id> <text>")
    snap = Snapshot(connect(start))
    issue_id = words[0]
    if issue_id not in snap.issues:
        print(f'Error: no issue found matching "{issue_id}"', file=sys.stderr)
        return 1
    rows = snap.comments.get(issue_id, [])
    if args.json:
        emit_json([{"id": c["id"], "issue_id": c["issue_id"], "author": c["author"],
                    "text": c["text"], "created_at": c["created_at"]} for c in rows])
        return 0
    if not rows:
        print(f"No comments on {issue_id}")
    for c in rows:
        print(f"[{c['author'] or ''}] {c['created_at']}\n  {c['text']}")
    return 0


def cmd_defer(args, start: Path) -> int:
    writer = open_writer(args, start)
    for issue_id in args.ids:
        if args.verb == "defer":
            if not args.until:
                raise Refused("defer needs --until <date>")
            writer.defer(issue_id, args.until)
        else:
            writer.undefer(issue_id)
    return report_issues(args, writer, args.ids,
                         "Deferred" if args.verb == "defer" else "Undeferred")


def cmd_delete(args, start: Path) -> int:
    if not args.force:
        raise Refused(f"delete is permanent; repeat with --force to delete {', '.join(args.ids)}")
    writer = open_writer(args, start)
    out = []
    for issue_id in args.ids:
        edges = writer.delete(issue_id)
        out.append({"id": issue_id, "deleted": True, "dependencies_removed": edges})
        if not args.json:
            print(f"✓ Deleted {issue_id} (removed {edges} dependency link(s))")
    if args.json:
        emit_json(out)
    return 0


def cmd_dep(args, start: Path) -> int:
    writer = open_writer(args, start)
    if args.action == "add":
        added = writer.dep_add(args.issue, args.depends_on, args.type)
        result = {"issue_id": args.issue, "depends_on_id": args.depends_on, "type": args.type,
                  "status": "added" if added else "exists"}
        text = (f"✓ Added dependency: {args.issue} depends on {args.depends_on} ({args.type})"
                if added else f"{args.issue} already depends on {args.depends_on} ({args.type})")
    else:
        removed = writer.dep_remove(args.issue, args.depends_on)
        result = {"issue_id": args.issue, "depends_on_id": args.depends_on,
                  "status": "removed", "removed": removed}
        text = f"✓ Removed dependency: {args.issue} no longer depends on {args.depends_on}"
    if args.json:
        emit_json(result)
    elif not args.quiet:
        print(text)
    return 0


def cmd_remember(args, start: Path) -> int:
    writer = open_writer(args, start)
    new, related, near = writer.remember(args.key or "", args.value, force=args.force)
    if args.json:
        emit_json({"key": args.key, "status": "created" if new else "updated"})
    elif not args.quiet:
        print(f"{'Remembered' if new else 'Updated'} [{args.key}]: {args.value[:PREVIEW]}")
    if near and not args.quiet:
        print(f"note: nearly duplicates memory '{near}' (stored: a short body is not refused); "
              f"if it adds to that memory, merge it in (cairn recall {near}, then cairn remember "
              f"--key {near} \"<merged>\") and forget this key", file=sys.stderr)
    if related and not args.quiet:
        links_hint = " ".join(f"[[{k}]]" for k in related)
        print(f"note: related to {links_hint} but links to no memory; add the link that "
              "holds (supersedes::, depends-on::, contradicts:: where the relation is causal) "
              f"with cairn remember --key {args.key}", file=sys.stderr)
    return 0


def cmd_forget(args, start: Path) -> int:
    writer = open_writer(args, start)
    old = writer.forget(args.key)
    if args.json:
        emit_json({"key": args.key, "status": "deleted"})
    elif not args.quiet:
        print(f"Forgot [{args.key}]: {old[:PREVIEW]}")
    return 0


# -- argument parsing -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Agent-facing work and memory store: a read-only shadow of bd, or a writable store of its own (cairn init).")
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
    p.add_argument("--claim", action="store_true",
                   help="claim the first ready issue (writable stores only)")
    p.add_argument("--actor")
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
    p.add_argument("--allocator", action="store_true",
                   help="write the allocator state (ids ever issued, child counters) as a seed "
                        "for `init --from ... --seed`, instead of rows")
    p.add_argument("--events", action="store_true",
                   help="write the event history (one JSON line per event, oldest first) for "
                        "`init --from ... --events`, instead of rows")
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

    # Write verbs: writable stores only (cairn init).
    def writer_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        w = sub.add_parser(name, help=help_text)
        w.add_argument("--actor", help="who writes (default: $BEADS_ACTOR)")
        # bd's global flags that agents type after the verb; meaningless here.
        w.add_argument("--sandbox", action="store_true", help=argparse.SUPPRESS)
        w.add_argument("-q", "--quiet", action="store_true", help="print nothing on success")
        return w

    p = writer_parser("init", "create a writable store in this directory")
    p.add_argument("-p", "--prefix", required=True, help="issue id prefix")
    p.add_argument("--from", dest="source", metavar="EXPORT",
                   help="build it from a `bd export --all` file (the cutover from bd)")
    p.add_argument("--seed", help="allocator seed from `cairn seed-from-dolt` (ids bd "
                                  "deleted, bd's child counters)")
    p.add_argument("--interactions", help="bd interactions.jsonl (becomes the event history)")
    p.add_argument("--events", help="a `cairn export --events` file (the tracked "
                                    ".beads/events.jsonl): the event history comes back")
    p.add_argument("--ledger", help="bd-memgraph ledger (memory timestamps and deletions)")
    p.add_argument("--hot", help="hot list: one memory key per line")
    p.add_argument("--bd-version", help="the bd version that wrote EXPORT (default: from "
                                        "the seed); recorded, not trusted for checks")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("migrate", help="bring a writable store to this build's schema")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("seed-from-dolt", help="read the allocator seed from a copy of "
                                              "bd's Dolt store")
    p.add_argument("source", help="the .beads directory or the Dolt repository in it")
    p.add_argument("--dolt", default="dolt", help="dolt executable (bd ships none)")
    p.add_argument("-o", "--output", help="write the seed here (default: stdout)")
    p.set_defaults(func=cmd_seed_from_dolt)

    def issue_fields(w: argparse.ArgumentParser) -> None:
        w.add_argument("-d", "--description")
        w.add_argument("--design")
        w.add_argument("--acceptance")
        w.add_argument("--notes")
        w.add_argument("-p", "--priority")
        w.add_argument("-t", "--type")
        w.add_argument("-a", "--assignee")

    p = writer_parser("create", "create an issue")
    p.add_argument("title_pos", nargs="?", metavar="title")
    p.add_argument("--title")
    issue_fields(p)
    p.add_argument("--parent")
    p.add_argument("--id")
    p.add_argument("-l", "--labels", action="append")
    p.add_argument("--body-file", help="read the description from a file ('-' = stdin)")
    p.add_argument("--defer", metavar="DATE", help="create deferred until DATE")
    p.add_argument("--silent", action="store_true", help="print only the new id")
    p.set_defaults(func=cmd_create)

    p = writer_parser("update", "change issues")
    p.add_argument("ids", nargs="+")
    p.add_argument("--title")
    issue_fields(p)
    p.add_argument("--append-notes")
    p.add_argument("-s", "--status")
    p.add_argument("--claim", action="store_true")
    p.add_argument("--add-label", action="append")
    p.add_argument("--remove-label", action="append")
    p.add_argument("--body-file", help="read the description from a file ('-' = stdin)")
    p.add_argument("--defer", metavar="DATE", help="defer until DATE (as `defer --until`)")
    p.set_defaults(func=cmd_update)

    p = writer_parser("close", "close issues")
    p.add_argument("ids", nargs="+")
    p.add_argument("-r", "--reason")
    p.add_argument("-f", "--force", action="store_true",
                   help="close even with open blockers")
    p.add_argument("--suggest-next", action="store_true",
                   help="after closing, list issues this close made ready")
    p.set_defaults(func=cmd_close)

    p = writer_parser("reopen", "reopen issues")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_reopen)

    p = writer_parser("note", "append a line to an issue's notes")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    p.add_argument("--file", help="read the note from a file ('-' = stdin)")
    p.set_defaults(func=cmd_note)

    p = writer_parser("comment", "add a comment")
    p.add_argument("id")
    p.add_argument("text")
    p.set_defaults(func=cmd_comment, action="add")

    p = writer_parser("comments", "comments <id> (list) | comments add <id> <text>")
    p.add_argument("words", nargs="+", metavar="add <id> <text> | <id>")
    p.set_defaults(func=cmd_comments)

    for name in ("defer", "undefer"):
        p = writer_parser(name, f"{name} issues")
        p.add_argument("ids", nargs="+")
        if name == "defer":
            p.add_argument("--until")
        p.set_defaults(func=cmd_defer)

    p = writer_parser("delete", "delete issues permanently (--force)")
    p.add_argument("ids", nargs="+")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_delete)

    p = writer_parser("dep", "add or remove a dependency")
    p.add_argument("action", choices=("add", "remove", "rm"))
    p.add_argument("issue")
    p.add_argument("depends_on")
    p.add_argument("--type", default="blocks")
    p.set_defaults(func=cmd_dep)

    p = writer_parser("remember", "set a memory (--key required)")
    p.add_argument("value")
    p.add_argument("--key")
    p.add_argument("--force", action="store_true",
                   help="store a body that is a near-duplicate of another key")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("sync", help="bring the store up to the tracked exports a pull brought in "
                       "(a fresh clone builds it); refused when two clones both wrote")
    p.add_argument("--status", action="store_true",
                   help="say equal / ahead / behind / diverged; exit 1 behind, 2 diverged")
    p.add_argument("-q", "--quiet", action="store_true", help="print nothing on success")
    p.set_defaults(func=cmd_sync)

    p = writer_parser("forget", "delete a memory")
    p.add_argument("key")
    p.set_defaults(func=cmd_forget)
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
    if verb in UNSUPPORTED_VERBS:
        return refuse_write(verb)

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
    except (StoreError, SourceError, shadow.BdError, Refused, WriteRefused) as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 2
    except WriteConflict as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
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
