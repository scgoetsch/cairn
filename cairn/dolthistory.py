"""Read bd's history out of its Dolt store as cairn events (`cairn history-from-dolt`).

bd keeps its history only in Dolt: every earlier memory body, every earlier issue text, who wrote
what. No bd export carries it, so a migration that reads only the export loses it, and the
people this tool is for (bd users of any version, leaving bd) cannot regenerate it.

What is read, from a COPY of the Dolt directory (as `seed-from-dolt` does), through Dolt's own
`dolt_diff_<table>` system tables, so only changes leave Dolt, never snapshots:

  issues        added -> `created`; each changed column -> `field_change` (notes appends
                as `appended`, eventdata); removed -> `deleted`
  config        `kv.memory.<key>` rows (where bd 1.1.2 keeps memories): added -> `memory_set`
                (old_hash null), modified -> `memory_set` with `old_value`, removed ->
                `memory_deleted` with the last body
  labels        added / removed -> `label_added` / `label_removed`
  dependencies  added / removed -> `dependency_added` / `dependency_removed`
  comments      added -> `comment_added`
  events        bd's audit table: read for the ACTOR of each commit (bd writes one actor per
                command, and a command is one commit), not imported as rows of its own

Every event is dated by its Dolt commit, carries `data.dolt_commit`, and has the id
`dolt-<hash of commit, table, key, field, op>`, so two runs give the same bytes. bd's
interactions.jsonl (the field-change log the cutover already imports) is merged in by time; a
change both sources hold is kept once, from interactions, which has the actor and the finer
timestamp. The output is an events file in `export --events` format, for `init --events`.

The manifest says what was read, what was carried, what was dropped as a duplicate, and what
stays in Dolt whatever this reader does (the commit graph, order inside one commit, the Dolt
committer, columns and tables cairn has no place for).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import timeutil
from .doltseed import DoltError, find_dolt_repo
from .eventdata import APPEND_FIELDS, field_change_data
from .importer import body_hash, read_jsonl
from .queries import go_json

MEMORY_PREFIX = "kv.memory."
# Issue columns cairn stores, minus the key and the ones that never change or move on every write.
DIFF_COLUMNS = ("title", "description", "design", "acceptance_criteria", "notes", "status",
                "priority", "issue_type", "assignee", "owner", "started_at", "closed_at",
                "close_reason", "defer_until")
ROW_COLUMNS = ("id", "title", "description", "design", "acceptance_criteria", "notes", "status",
               "priority", "issue_type", "assignee", "owner", "created_at", "created_by",
               "updated_at", "started_at", "closed_at", "close_reason", "defer_until")
DATETIME_COLUMNS = frozenset({"created_at", "updated_at", "started_at", "closed_at",
                              "defer_until"})
REQUIRED_TABLES = ("issues", "config", "dolt_log")
READ_TABLES = ("issues", "config", "labels", "dependencies", "comments", "events")
# A change in interactions.jsonl and the same change seen in Dolt are one change when their
# times are this close (measured: the interaction is written, then bd commits, ~30 ms later).
SAME_CHANGE = timedelta(seconds=5)
_DOLT_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?$")

Query = Callable[[str], List[Dict[str, Any]]]


class HistoryError(DoltError):
    """The history cannot be read completely; nothing is written."""


@contextlib.contextmanager
def dolt_copy(source: Path, dolt: str = "dolt") -> Iterator[Tuple[Path, Query]]:
    """Copy the Dolt repository under `source` to a temporary directory and yield
    (repository, query): the live store is never opened."""
    if shutil.which(dolt) is None:
        raise DoltError(f"dolt CLI not found ({dolt}); bd ships none: install a dolt "
                        "release and pass --dolt PATH")
    repo = find_dolt_repo(source)
    with tempfile.TemporaryDirectory(prefix="cairn-dolt-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(repo, copy, symlinks=True)
        home = Path(tmp) / "home"         # dolt wants a user config; never touch ~/.dolt
        home.mkdir()
        env = {**os.environ, "HOME": str(home), "DOLT_ROOT_PATH": str(home)}
        for key, value in (("user.name", "cairn"), ("user.email", "cairn@localhost")):
            subprocess.run([dolt, "config", "--global", "--add", key, value], env=env,
                           capture_output=True, check=False)

        def query(sql: str) -> List[Dict[str, Any]]:
            proc = subprocess.run([dolt, "sql", "-r", "json", "-q", sql], cwd=copy, env=env,
                                  capture_output=True, text=True, timeout=900)
            if proc.returncode != 0:
                raise DoltError(f"dolt sql failed: {(proc.stderr or proc.stdout).strip()[:300]}")
            out = proc.stdout.strip()
            return json.loads(out).get("rows", []) if out else []

        yield repo, query


def iso(stamp: Any, *, fraction: bool) -> Optional[str]:
    """Dolt's `YYYY-MM-DD HH:MM:SS[.ffffff]` (UTC, no zone; measured: a status change in
    interactions.jsonl and its Dolt commit are 26 ms apart) as cairn writes it: whole seconds for
    a column value, milliseconds for an event time."""
    if stamp in (None, ""):
        return None
    match = _DOLT_STAMP.match(str(stamp))
    if not match:
        return str(stamp)
    day, clock, frac = match.groups()
    if fraction:
        return f"{day}T{clock}.{(frac or '.0')[1:4].ljust(3, '0')}Z"
    return f"{day}T{clock}Z"


def column_value(column: str, value: Any) -> Any:
    """A Dolt column value as cairn stores it: empty text is NULL, times are ISO seconds."""
    if value is None or value == "":
        return None
    if column in DATETIME_COLUMNS:
        return iso(value, fraction=False)
    if column == "priority":
        return int(value)
    return value


def comparable(value: Any) -> Optional[str]:
    """interactions.jsonl stores priority as a string; compare as text, empty as None."""
    return None if value in (None, "") else str(value)


def _event_id(*parts: Any) -> str:
    return "dolt-" + hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:24]


class Reader:
    def __init__(self, query: Query) -> None:
        self.q = query
        self.manifest: Dict[str, Any] = {"read": {}, "carried": Counter(), "dropped": Counter(),
                                         "left_behind": [], "warnings": []}

    # -- the commit timeline ----------------------------------------------------------------

    def commits(self) -> Dict[str, Tuple[int, str]]:
        rows = self.q("SELECT commit_hash, committer, date FROM dolt_log")
        merges = self.q("SELECT commit_hash FROM dolt_commit_ancestors "
                        "GROUP BY commit_hash HAVING COUNT(*) > 1")
        if merges:
            raise HistoryError(f"the Dolt history has {len(merges)} merge commit(s); this reader "
                               "handles a linear history only")
        ordered = sorted(rows, key=lambda r: (r["date"], r["commit_hash"]))
        self.manifest["read"]["commits"] = len(rows)
        self.manifest["committers"] = sorted({r["committer"] for r in rows})
        return {r["commit_hash"]: (i, iso(r["date"], fraction=True) or "")
                for i, r in enumerate(ordered)}

    def actors(self) -> Dict[str, str]:
        """The actor of each commit, from the rows bd's events table gained in it; a commit
        with no such row, or with more than one actor, has none."""
        rows = self.q("SELECT to_commit, to_actor FROM dolt_diff_events "
                      "WHERE diff_type = 'added' AND to_commit <> 'WORKING'")
        seen: Dict[str, set] = defaultdict(set)
        for r in rows:
            if r.get("to_actor"):
                seen[r["to_commit"]].add(r["to_actor"])
        self.manifest["read"]["bd_events_added"] = len(rows)
        many = sum(1 for a in seen.values() if len(a) > 1)
        if many:
            self.manifest["warnings"].append(f"{many} commit(s) carry more than one actor; "
                                             "their events have none")
        return {c: next(iter(a)) for c, a in seen.items() if len(a) == 1}

    # -- per table -------------------------------------------------------------------------

    def diff(self, table: str, columns: Sequence[str]) -> List[Dict[str, Any]]:
        cols = ", ".join(f"{side}_{c}" for c in columns for side in ("from", "to"))
        rows = self.q(f"SELECT diff_type, to_commit, from_commit, {cols} "  # noqa: S608 — names are module constants
                      f"FROM dolt_diff_{table} WHERE to_commit <> 'WORKING'")
        self.manifest["read"][f"{table}_changes"] = len(rows)
        return rows


def read_history(query: Query, *, export_memory_keys: Sequence[str] = ()) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The Dolt history as cairn event objects (unordered against interactions; each carries
    `_order`, the commit index) and the manifest."""
    r = Reader(query)
    tables = {next(iter(row.values())) for row in query("SHOW TABLES")}
    missing = [t for t in REQUIRED_TABLES if t not in tables and t != "dolt_log"]
    if missing:
        raise HistoryError(f"the Dolt store has no {', '.join(missing)} table: not a bd store "
                           "this reader knows (bd 1.1.2's schema)")
    issue_cols = {row["Field"] for row in query("DESCRIBE issues")}
    diff_cols = [c for c in DIFF_COLUMNS if c in issue_cols]
    row_cols = [c for c in ROW_COLUMNS if c in issue_cols]
    r.manifest["issue_columns_not_carried"] = sorted(issue_cols - set(ROW_COLUMNS))
    r.manifest["tables_not_read"] = sorted(t for t in tables
                                           if t not in READ_TABLES and not t.startswith("dolt_"))
    commits = r.commits()
    actors = r.actors() if "events" in tables else {}
    out: List[Dict[str, Any]] = []
    per_commit: Counter = Counter()

    def emit(commit: str, kind: str, data: Dict[str, Any], *, issue_id: Optional[str] = None,
             memory_key: Optional[str] = None, actor: Optional[str] = None,
             key: Sequence[Any] = ()) -> None:
        order, stamp = commits[commit]
        data = {**data, "dolt_commit": commit}
        out.append({"_type": "event",
                    "id": _event_id(commit, kind, issue_id or "", memory_key or "", *key),
                    "kind": kind, "issue_id": issue_id, "memory_key": memory_key,
                    "actor": actor if actor is not None else actors.get(commit),
                    "created_at": stamp, "data": data, "_order": order})
        r.manifest["carried"][kind] += 1
        per_commit[commit] += 1

    # parents: a parent-child dependency added in the commit that created the child
    parents: Dict[Tuple[str, str], str] = {}
    dep_rows: List[Dict[str, Any]] = []
    if "dependencies" in tables:
        dep_cols = {row["Field"] for row in query("DESCRIBE dependencies")}
        target = "depends_on_issue_id" if "depends_on_issue_id" in dep_cols else "depends_on_id"
        dep_rows = r.diff("dependencies", ("issue_id", target, "type"))
        for d in dep_rows:
            if d["diff_type"] == "added" and d.get("to_type") == "parent-child":
                parents[(d["to_issue_id"], d["to_commit"])] = d[f"to_{target}"]

    # issues
    notes_seen: set = set()
    for d in sorted(r.diff("issues", row_cols), key=lambda d: commits[d["to_commit"]][0]):
        commit, kind = d["to_commit"], d["diff_type"]
        if kind == "added":
            issue_id = d["to_id"]
            emit(commit, "created", {"title": d.get("to_title"),
                                     "parent": parents.get((issue_id, commit))},
                 issue_id=issue_id, key=("created",))
        elif kind == "modified":
            issue_id = d["to_id"]
            for col in diff_cols:
                old = column_value(col, d.get(f"from_{col}"))
                new = column_value(col, d.get(f"to_{col}"))
                if old == new:
                    continue
                # Notes set at creation are in no event (as in cairn): the first notes change
                # of an issue is a full snapshot, later ones may be appends (eventdata).
                anchored = col not in APPEND_FIELDS or (issue_id, col) in notes_seen
                data = field_change_data(col, old, new, anchored=anchored)
                if col in APPEND_FIELDS:
                    notes_seen.add((issue_id, col))
                emit(commit, "field_change", data, issue_id=issue_id, key=(col,))
        elif kind == "removed":
            issue_id = d["from_id"]
            row = {c: column_value(c, d.get(f"from_{c}")) for c in row_cols}
            emit(commit, "deleted", {"issue": row, "labels": [], "comments": [],
                                     "dependencies": []}, issue_id=issue_id, key=("deleted",))
            r.manifest["left_behind"].append(
                f"deleted issue {issue_id}: its labels, comments and dependencies at deletion")

    # memories (and a count of the other config keys, which cairn has no place for)
    other_config = 0
    found_keys: set = set()
    for d in sorted(r.diff("config", ("key", "value")), key=lambda d: commits[d["to_commit"]][0]):
        name = d.get("to_key") or d.get("from_key") or ""
        if not name.startswith(MEMORY_PREFIX):
            other_config += 1
            continue
        key = name[len(MEMORY_PREFIX):]
        found_keys.add(key)
        commit, kind = d["to_commit"], d["diff_type"]
        old, new = d.get("from_value"), d.get("to_value")
        if kind == "added":
            emit(commit, "memory_set", {"old_hash": None, "new_hash": body_hash(new),
                                        "old_value": None}, memory_key=key, key=("set",))
        elif kind == "modified":
            if old == new:
                continue
            emit(commit, "memory_set", {"old_hash": body_hash(old), "new_hash": body_hash(new),
                                        "old_value": old}, memory_key=key, key=("set",))
        elif kind == "removed":
            emit(commit, "memory_deleted", {"old_hash": body_hash(old), "old_value": old},
                 memory_key=key, key=("deleted",))
    r.manifest["config_changes_not_memories"] = other_config
    absent = sorted(set(export_memory_keys) - found_keys)
    if export_memory_keys and not found_keys:
        raise HistoryError("the export has memories and the Dolt history has none under "
                           f"config.{MEMORY_PREFIX}*: this bd keeps memories elsewhere; refusing "
                           "to write a history without them")
    if absent:
        r.manifest["warnings"].append(f"{len(absent)} memory key(s) of the export have no Dolt "
                                      f"history: {', '.join(absent[:5])}")

    # labels, dependencies, comments
    if "labels" in tables:
        for d in r.diff("labels", ("issue_id", "label")):
            added = d["diff_type"] == "added"
            if d["diff_type"] not in ("added", "removed"):
                continue
            side = "to" if added else "from"
            commit = d["to_commit"]
            emit(commit, "label_added" if added else "label_removed",
                 {"label": d[f"{side}_label"]}, issue_id=d[f"{side}_issue_id"],
                 key=(d[f"{side}_label"],))
    for d in dep_rows:
        if d["diff_type"] not in ("added", "removed"):
            continue
        side = "to" if d["diff_type"] == "added" else "from"
        target = next(k for k in d if k.startswith(f"{side}_depends_on"))
        emit(d["to_commit"], f"dependency_{d['diff_type']}",
             {"depends_on_id": d[target], "type": d[f"{side}_type"]},
             issue_id=d[f"{side}_issue_id"], key=(d[target], d[f"{side}_type"]))
    if "comments" in tables:
        for d in r.diff("comments", ("id", "issue_id", "author")):
            if d["diff_type"] == "added":
                emit(d["to_commit"], "comment_added", {"comment_id": d["to_id"]},
                     issue_id=d["to_issue_id"], actor=d.get("to_author"), key=(d["to_id"],))
            else:
                r.manifest["dropped"][f"comment_{d['diff_type']}"] += 1

    m = r.manifest
    m["max_events_in_one_commit"] = max(per_commit.values(), default=0)
    m["events_without_actor"] = sum(1 for e in out if not e["actor"])
    m["left_behind"][:0] = [
        "the Dolt commit graph: hashes and dates ride on the events (data.dolt_commit), parent "
        "links and commit messages stay in Dolt",
        "the order of changes inside one commit (events of one commit are in table, then "
        "column order)",
        f"the Dolt committer ({', '.join(m['committers'])}): never copied into actor",
        "bd's events table as rows: read for the actor of each commit only",
        "changes made between two commits (only committed states exist)",
    ]
    return out, m


def interactions_events(path: Path) -> List[Dict[str, Any]]:
    """bd's interactions.jsonl as the event objects `init --interactions` stores."""
    events = []
    for _, row in read_jsonl(path):
        extra = row.get("extra") or {}
        events.append({"_type": "event", "id": row["id"], "kind": row["kind"],
                       "issue_id": row.get("issue_id"), "memory_key": None,
                       "actor": row.get("actor"), "created_at": row["created_at"],
                       "data": extra})
    return events


def merge(dolt: List[Dict[str, Any]], earlier: List[Dict[str, Any]],
          manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Dolt events (commit order) and already-recorded events (`earlier`, their own order) as
    one history in time order. A Dolt field_change that an earlier event already records
    (same issue, field, old and new, within SAME_CHANGE) is dropped: the earlier one has the
    actor and the finer time."""
    index: Dict[Tuple[Any, Any], List[Tuple[datetime, Any, Any, int]]] = defaultdict(list)
    for i, e in enumerate(earlier):
        if e["kind"] == "field_change":
            when = timeutil.parse(e["created_at"])
            if when is not None:
                index[(e["issue_id"], e["data"].get("field"))].append(
                    (when, comparable(e["data"].get("old_value")),
                     comparable(e["data"].get("new_value")), i))
    used: set = set()
    kept = []
    for e in sorted(dolt, key=lambda e: e["_order"]):
        if e["kind"] == "field_change" and "appended" not in e["data"]:
            when = timeutil.parse(e["created_at"])
            match = next((i for t, o, n, i in index.get((e["issue_id"], e["data"]["field"]), ())
                          if i not in used and when is not None and abs(t - when) <= SAME_CHANGE
                          and o == comparable(e["data"].get("old_value"))
                          and n == comparable(e["data"].get("new_value"))), None)
            if match is not None:
                used.add(match)
                manifest["dropped"]["field_change already in the earlier history"] += 1
                manifest["carried"]["field_change"] -= 1
                continue
        kept.append(e)
    manifest["earlier_events_matched"] = len(used)
    merged: List[Dict[str, Any]] = []
    i = 0
    for e in kept:
        when = timeutil.parse(e["created_at"])
        while i < len(earlier) and (when is None or
                                    (timeutil.parse(earlier[i]["created_at"]) or when) <= when):
            merged.append(earlier[i])
            i += 1
        merged.append(e)
    merged.extend(earlier[i:])
    for e in merged:
        e.pop("_order", None)
    return merged


def write_events(events: List[Dict[str, Any]], path: Path) -> None:
    from .queries import EVENT_KEYS
    with path.open("w", encoding="utf-8") as out:
        for e in events:
            data = json.loads(json.dumps(e["data"], sort_keys=True))
            out.write(go_json({"_type": "event", **{k: (data if k == "data" else e.get(k))
                                                     for k in EVENT_KEYS}}) + "\n")


def manifest_json(manifest: Dict[str, Any]) -> str:
    return json.dumps({k: (dict(v) if isinstance(v, Counter) else v)
                       for k, v in manifest.items()}, indent=2, sort_keys=True)
