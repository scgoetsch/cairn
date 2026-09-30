"""Shadow mode: mirror a beads store and check that cairn answers like bd.

bd is the only writer. `sync` rebuilds the cairn database from a fresh
`bd export --all`; `diff` asks bd the same questions through bd's own read
commands and compares. The two read paths are independent on purpose: the diff
catches an import bug (wrong data) as well as a semantics bug (wrong answer).

A run has three outcomes, and the log keeps them apart:
  clean           cairn and bd agree, and nothing was left out of the import
  not clean       they disagree, or rows could not be imported
  could not run   bd missing, a timeout, unreadable input — evidence of nothing;
                  it neither counts towards the soak nor resets it

Another session can write to bd between the export and the comparison queries.
A run that finds differences therefore repeats once, and reports only what is
still different the second time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import timeutil
from .importer import (DERIVED_FIELDS, ImportReport, SourceError, body_hash,
                       build_store, read_jsonl)
from .queries import Snapshot, memories as all_memories
from .store import StoreError, db_path, load_config, open_db

LOG_NAME = "shadow-log.jsonl"
# A session-start hook gives its checks about 20 seconds and discards the output
# of one it has to kill. Staying inside that budget is what keeps a hang loud.
DEFAULT_DEADLINE_SECONDS = 15.0
MANUAL_DEADLINE_SECONDS = 120.0
SHOW_SAMPLE = 3            # issues compared through `bd show` per run, rotating
MAX_REPORTED = 10          # per-issue messages per comparison before summarising
# Exit criteria for the shadow stage: ten clean runs AND two weeks.
SOAK_MIN_CLEAN_RUNS = 10
SOAK_MIN_DAYS = 14
# Keys cairn adds to `show --json` that bd does not print.
SHOW_EXTRAS = ("dependents", "comments")


class BdError(Exception):
    """bd could not be run, failed, timed out, or printed something that is not JSON."""


class Deadline:
    """A time budget shared by every bd call of one run."""

    def __init__(self, seconds: Optional[float]) -> None:
        self.seconds = seconds
        self._end = None if seconds is None else time.monotonic() + seconds

    def remaining(self) -> Optional[float]:
        if self._end is None:
            return None
        left = self._end - time.monotonic()
        if left <= 0:
            raise BdError(f"the {self.seconds:g}s deadline passed before the run finished")
        return left


@dataclass
class RunResult:
    report: ImportReport
    differences: List[str] = field(default_factory=list)
    retried: bool = False

    @property
    def clean(self) -> bool:
        return not self.differences and not self.report.losses


def find_workspace(start: Path) -> Path:
    """The directory holding the `.beads` store, walking up from `start`. bd is
    run from here, wherever the cairn store itself lives."""
    here = start.resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".beads").is_dir():
            return candidate
    raise StoreError(f"no .beads/ store found from {start} upwards")


def run_bd(args: List[str], cwd: Path, bd: str = "bd",
           deadline: Optional[Deadline] = None) -> str:
    timeout = deadline.remaining() if deadline else None
    try:
        proc = subprocess.run([bd, *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise BdError(f"'{bd}' is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BdError(f"bd {' '.join(args)} did not finish within the "
                      f"{deadline.seconds:g}s deadline") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise BdError(f"bd {' '.join(args)} exited {proc.returncode}: "
                      f"{detail[0] if detail else 'no output'}")
    return proc.stdout


def bd_json(args: List[str], cwd: Path, bd: str = "bd",
            deadline: Optional[Deadline] = None) -> Any:
    out = run_bd([*args, "--json"], cwd, bd, deadline)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise BdError(f"bd {' '.join(args)} --json did not print JSON ({exc.msg})") from exc


# -- comparison helpers ------------------------------------------------------

def _normal(obj: Dict[str, Any]) -> Dict[str, Any]:
    """An issue object with order-insensitive parts sorted and empty values
    dropped, so two serialisations of the same issue compare equal."""
    out: Dict[str, Any] = {}
    for key, value in obj.items():
        if value is None or value == "" or value == []:
            continue
        if key == "dependencies" and isinstance(value, list):
            value = sorted((_normal(v) if isinstance(v, dict) else v for v in value),
                           key=lambda v: json.dumps(v, sort_keys=True))
        elif key in ("labels", "blocked_by") and isinstance(value, list):
            value = sorted(value)
        out[key] = value
    return out


def _fields_differing(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    a, b = _normal(a), _normal(b)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def _capped(messages: List[str], what: str) -> List[str]:
    if len(messages) <= MAX_REPORTED:
        return messages
    return messages[:MAX_REPORTED] + [f"… and {len(messages) - MAX_REPORTED} more {what}"]


def _issues(value: Any, what: str) -> List[Dict[str, Any]]:
    """bd's answer must be a list of objects that each carry a string id; an
    unexpected shape is a run that could not happen, not a crash."""
    if (not isinstance(value, list)
            or not all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in value)):
        raise BdError(f"bd {what} --json did not print a list of issues")
    return value


def _set_diff(label: str, in_bd: set, in_cairn: set) -> List[str]:
    out = []
    if in_bd - in_cairn:
        out.append(f"{label}: only in bd: {sorted(in_bd - in_cairn)}")
    if in_cairn - in_bd:
        out.append(f"{label}: only in cairn: {sorted(in_cairn - in_bd)}")
    return out


# -- sync ---------------------------------------------------------------------

def remove_stale_temp_files(store_dir: Path, older_than_seconds: float = 600) -> None:
    """A run that was killed outright leaves its temporary export (a full copy
    of the memories) and half-built database behind. Clear old ones; leave young
    ones alone, they may belong to a run in another session."""
    cutoff = time.time() - older_than_seconds
    for pattern in ("bd-export.tmp-*", "cairn.db.tmp-*"):
        for path in store_dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

def roundtrip_differences(conn: sqlite3.Connection, export_path: Path) -> List[str]:
    """Every supported row of the source export must come back out of cairn's
    own export unchanged. Catches a field the importer dropped or mangled."""
    snap = Snapshot(conn)
    stored = {r["key"]: r["value"] for r in all_memories(conn)}
    messages: List[str] = []
    for _, row in read_jsonl(export_path):
        kind = row.get("_type", "issue")
        if kind == "issue":
            if row["id"] not in snap.issues:
                messages.append(f"round trip: issue {row['id']} is missing from the store")
                continue
            mine = snap.issue_object(row["id"], shape="export")
            for counter in DERIVED_FIELDS:       # compared when the source states them
                if counter not in row:
                    mine.pop(counter, None)
            fields = _fields_differing(row, mine)
            if fields:
                messages.append(f"round trip: issue {row['id']} differs in {fields}")
        elif kind == "memory" and stored.get(row["key"]) != row["value"]:
            messages.append(f"round trip: memory {row['key']} differs")
    return _capped(messages, "round-trip differences")


def sync(store_dir: Path, workspace: Path, bd: str = "bd",
         deadline: Optional[Deadline] = None) -> Tuple[ImportReport, List[str]]:
    """Rebuild the store from bd. Returns the import report and any round-trip
    differences between bd's export and cairn's re-export of it."""
    store_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_temp_files(store_dir)
    optional = (
        ("interactions_path", workspace / ".beads" / "interactions.jsonl",
         "history was not imported"),
        ("ledger_path", workspace / ".beads" / "memgraph-ledger.json",
         "memories have no timestamps or deletion records"),
        ("hot_path", workspace / ".claude" / "memory-hot.txt",
         "no memory is in the hot tier"),
    )
    export = store_dir / f"bd-export.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl"
    try:
        run_bd(["export", "--all", "-o", str(export)], workspace, bd, deadline)
        if not export.is_file():
            raise BdError("bd export exited 0 but wrote no file")
        report = build_store(
            db_path(store_dir), export,
            source_label=f"bd export --all ({workspace})",
            extra_prefixes=load_config(store_dir).get("issue_prefixes", []),
            **{name: path for name, path, _ in optional})
        for _, path, consequence in optional:
            if not path.is_file():         # say so: an absent input is not an empty one
                report.warnings.append(
                    f"no {path.relative_to(workspace)} — {consequence}")
        conn = open_db(db_path(store_dir))
        try:
            return report, roundtrip_differences(conn, export)
        finally:
            conn.close()
    finally:
        if export.exists():
            export.unlink()


# -- diff ---------------------------------------------------------------------

def diff(store_dir: Path, workspace: Path, bd: str = "bd", now: Optional[str] = None,
         deadline: Optional[Deadline] = None, sample_offset: int = 0) -> List[str]:
    """Compare cairn's answers with bd's. Returns human-readable differences."""
    now = now or timeutil.now_iso()
    conn = open_db(db_path(store_dir))
    try:
        snap = Snapshot(conn)
        differences: List[str] = []

        bd_ready = [i["id"] for i in _issues(
            bd_json(["ready", "--limit", "0"], workspace, bd, deadline), "ready")]
        ours = snap.ready(now=now)
        differences += _set_diff("ready", set(bd_ready), set(ours))
        repeated = sorted({i for i in bd_ready if bd_ready.count(i) > 1})
        if repeated:
            differences.append(f"ready: bd lists these ids more than once: {repeated}")
        # Order is compared over the ids both sides returned, so a set mismatch
        # cannot hide an ordering bug.
        theirs_common = [i for i in dict.fromkeys(bd_ready) if i in set(ours)]
        ours_common = [i for i in ours if i in set(bd_ready)]
        first = next((i for i, (a, b) in enumerate(zip(theirs_common, ours_common)) if a != b),
                     None)
        if first is not None:
            differences.append(f"ready order: differs from position {first} "
                               f"(bd {theirs_common[first]}, cairn {ours_common[first]})")

        bd_in_progress = {i["id"] for i in _issues(bd_json(
            ["list", "--status=in_progress", "--limit", "0"], workspace, bd, deadline),
            "list --status=in_progress")}
        differences += _set_diff("in_progress", bd_in_progress,
                                 set(snap.select(statuses=["in_progress"])))

        # `bd blocked` has no limit flag; it returns every blocked issue.
        bd_blocked = {i["id"]: set(i.get("blocked_by") or [])
                      for i in _issues(bd_json(["blocked"], workspace, bd, deadline),
                                       "blocked")}
        our_blocked = {k: set(v) for k, v in snap.blocked().items()}
        differences += _set_diff("blocked", set(bd_blocked), set(our_blocked))
        for issue_id in sorted(set(bd_blocked) & set(our_blocked)):
            if bd_blocked[issue_id] != our_blocked[issue_id]:
                differences.append(
                    f"blocked_by of {issue_id}: bd {sorted(bd_blocked[issue_id])}, "
                    f"cairn {sorted(our_blocked[issue_id])}")

        # Every field of every issue, not only its status.
        bd_all = {i["id"]: i for i in _issues(
            bd_json(["list", "--all", "--limit", "0"], workspace, bd, deadline), "list --all")}
        differences += _set_diff("issues", set(bd_all), set(snap.issues))
        common = sorted(set(bd_all) & set(snap.issues))
        per_issue = []
        for issue_id in common:
            fields = _fields_differing(bd_all[issue_id], snap.issue_object(issue_id))
            if fields:
                per_issue.append(f"issue {issue_id}: bd and cairn differ in {fields}")
        differences += _capped(per_issue, "issues differing")

        # `show` has its own shape and its own counting rule; one bd call per
        # issue is too slow for all of them, so each run checks a rotating few.
        per_show = []
        for n in range(min(SHOW_SAMPLE, len(common))):
            issue_id = common[(sample_offset + n) % len(common)]
            shown = bd_json(["show", issue_id], workspace, bd, deadline)
            theirs = shown[0] if isinstance(shown, list) and shown else {}
            mine = {k: v for k, v in snap.issue_object(issue_id, shape="show").items()
                    if k not in SHOW_EXTRAS}
            fields = _fields_differing(theirs, mine)
            if fields:
                per_show.append(f"show {issue_id}: bd and cairn differ in {fields}")
        differences += per_show

        answered = bd_json(["memories"], workspace, bd, deadline)
        if not isinstance(answered, dict):
            raise BdError("bd memories --json did not print an object")
        bd_memories = {k: v for k, v in answered.items() if k != "schema_version"}
        if not all(isinstance(v, str) for v in bd_memories.values()):
            raise BdError("bd memories --json printed a value that is not a string")
        our_memories = {r["key"]: r["body_hash"] for r in all_memories(conn)}
        differences += _set_diff("memory keys", set(bd_memories), set(our_memories))
        for key in sorted(set(bd_memories) & set(our_memories)):
            if body_hash(bd_memories[key]) != our_memories[key]:
                differences.append(f"memory body differs: {key}")
        return differences
    finally:
        conn.close()


# -- run + log ----------------------------------------------------------------

def _append_log(store_dir: Path, entry: Dict[str, Any]) -> None:
    """One line, one write() on an O_APPEND descriptor: concurrent sessions
    cannot interleave inside a line."""
    store_dir.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(store_dir / LOG_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read_log(store_dir: Path) -> Tuple[List[Dict[str, Any]], int]:
    """(entries, unreadable_line_count). A damaged line is counted, not fatal."""
    log = store_dir / LOG_NAME
    entries: List[Dict[str, Any]] = []
    unreadable = 0
    if log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            if isinstance(entry, dict) and timeutil.parse(entry.get("at")) is not None:
                entries.append(entry)
            else:
                unreadable += 1
    return entries, unreadable


def run(store_dir: Path, workspace: Path, bd: str = "bd",
        deadline_seconds: Optional[float] = DEFAULT_DEADLINE_SECONDS) -> RunResult:
    """sync, diff, and append the outcome to the soak log."""
    started = time.monotonic()
    deadline = Deadline(deadline_seconds)
    offset = len(read_log(store_dir)[0]) * SHOW_SAMPLE

    def attempt() -> Tuple[ImportReport, List[str]]:
        report, roundtrip = sync(store_dir, workspace, bd, deadline)
        return report, roundtrip + diff(store_dir, workspace, bd, deadline=deadline,
                                        sample_offset=offset)

    elapsed = lambda: int((time.monotonic() - started) * 1000)
    try:
        report, differences = attempt()
        retried = bool(differences)
        if retried:
            report, differences = attempt()
    except (BdError, SourceError, StoreError, OSError, sqlite3.Error) as exc:
        _append_log(store_dir, {"at": timeutil.now_iso(), "ok": None,
                                "error": str(exc)[:300], "ms": elapsed()})
        raise

    result = RunResult(report, differences, retried)
    entry: Dict[str, Any] = {
        "at": timeutil.now_iso(),
        "ok": result.clean,
        "differences": len(differences),
        "losses": len(report.losses),
        "warnings": len(report.warnings),
        "issues": report.counts.get("issues", 0),
        "memories": report.counts.get("memories", 0),
        "ms": elapsed(),
    }
    if retried:
        entry["retried"] = True
    if not result.clean:
        entry["first"] = (differences + report.losses)[:5]
    _append_log(store_dir, entry)
    return result


def status(store_dir: Path) -> Dict[str, Any]:
    """Summarise the soak log against the exit criteria."""
    entries, unreadable = read_log(store_dir)
    streak: List[Dict[str, Any]] = []
    for entry in reversed(entries):
        if entry.get("ok") is None:        # could not run: neither clean nor a failure
            continue
        if not entry["ok"]:
            break
        streak.append(entry)
    streak.reverse()
    seconds = 0.0
    if streak:
        seconds = (timeutil.parse(streak[-1]["at"])
                   - timeutil.parse(streak[0]["at"])).total_seconds()
    days = round(seconds / 86400, 1)          # for display only
    failures = [e for e in entries if e.get("ok") is False]
    errors = [e for e in entries if e.get("ok") is None]
    return {
        "runs": len(entries),
        "clean_streak": len(streak),
        "clean_streak_days": days,
        "clean_streak_since": streak[0]["at"] if streak else None,
        "last_run": entries[-1]["at"] if entries else None,
        "failed_runs": len(failures),
        "last_failure": failures[-1] if failures else None,
        "could_not_run": len(errors),
        "last_error": errors[-1] if errors else None,
        "unreadable_log_lines": unreadable,
        "criteria": f"{SOAK_MIN_CLEAN_RUNS} consecutive clean runs spanning {SOAK_MIN_DAYS} days",
        # the unrounded span: 13 days 23 hours displays as 14.0 but is not 14 days
        "criteria_met": (len(streak) >= SOAK_MIN_CLEAN_RUNS
                         and seconds >= SOAK_MIN_DAYS * 86400),
    }
