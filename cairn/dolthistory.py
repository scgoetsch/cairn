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
                "priority", "issue_type", "assignee", "owner", "created_at", "created_by",
                "started_at", "closed_at", "close_reason", "defer_until")
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
    beads = source.resolve()
    beads = beads / ".beads" if (beads / ".beads").is_dir() else beads
    both = [d for d in ("embeddeddolt", "dolt") if (beads / d).is_dir()
            and any((p / ".dolt").is_dir() for p in (beads / d).iterdir())]
    if len(both) > 1:
        raise HistoryError(f"{beads} holds an embedded and a server-mode Dolt store; name the "
                           "repository of the one bd uses now")
    with tempfile.TemporaryDirectory(prefix="cairn-dolt-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(repo, copy, symlinks=False)   # a link would still reach the live store
        home = Path(tmp) / "home"         # dolt wants a user config; never touch ~/.dolt
        home.mkdir()
        env = {**os.environ, "HOME": str(home), "DOLT_ROOT_PATH": str(home), "TZ": "UTC"}
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


# bd's auto-commit message names the actor that wrote it ("bd: note (auto-commit) by alice
# [proj-1]"), measured on bd 1.1.2, 1.3.1 and 1.3.4. Used where bd's events table names none.
AUTO_COMMIT_ACTOR = re.compile(r"\(auto-commit\) by (\S+)(?= \[|$)")


class Reader:
    def __init__(self, query: Query) -> None:
        self.q = query
        self.manifest: Dict[str, Any] = {"read": {}, "carried": Counter(), "dropped": Counter(),
                                         "left_behind": [], "warnings": []}

    # -- the commit timeline ----------------------------------------------------------------

    def commits(self) -> Dict[str, Tuple[int, str]]:
        rows = self.q("SELECT commit_hash, committer, date, message FROM dolt_log")
        self.messages = {r["commit_hash"]: r.get("message") or "" for r in rows}
        merges = self.q("SELECT commit_hash FROM dolt_commit_ancestors "
                        "GROUP BY commit_hash HAVING COUNT(*) > 1")
        if merges:
            raise HistoryError(f"the Dolt history has {len(merges)} merge commit(s); this reader "
                               "handles a linear history only")
        ordered = sorted(rows, key=lambda r: (r["date"], r["commit_hash"]))
        self.manifest["read"]["commits"] = len(rows)
        self.manifest["committers"] = sorted({r["committer"] for r in rows})
        timeline = {r["commit_hash"]: (i, iso(r["date"], fraction=True) or "")
                    for i, r in enumerate(ordered)}
        bad = [h for h, (_, stamp) in timeline.items() if timeutil.parse(stamp) is None]
        if bad:
            raise HistoryError(f"{len(bad)} Dolt commit date(s) this reader cannot parse "
                               f"(first: {bad[0]}); refusing rather than misplace events")
        return timeline

    def actors(self) -> Dict[str, str]:
        """The actor of each commit, from the rows bd's events table gained in it; a commit
        with no such row, or with more than one actor, has none. Keeps every row's commit and
        type for the per-row disposition in the manifest (`bd_events`)."""
        rows = self.q("SELECT diff_type, to_commit, to_actor, to_event_type, from_event_type "
                      "FROM dolt_diff_events WHERE to_commit <> 'WORKING'")
        self.bd_events = rows
        seen: Dict[str, set] = defaultdict(set)
        for r in rows:
            if r["diff_type"] == "added" and r.get("to_actor"):
                seen[r["to_commit"]].add(r["to_actor"])
        self.manifest["read"]["bd_events_changes"] = len(rows)
        many = sum(1 for a in seen.values() if len(a) > 1)
        if many:
            self.manifest["warnings"].append(f"{many} commit(s) carry more than one actor; "
                                             "their events have none")
        self.ambiguous = {c for c, a in seen.items() if len(a) > 1}
        return {c: next(iter(a)) for c, a in seen.items() if len(a) == 1}

    # -- per table -------------------------------------------------------------------------

    def diff(self, table: str, columns: Sequence[str]) -> List[Dict[str, Any]]:
        cols = ", ".join(f"{side}_{c}" for c in columns for side in ("from", "to"))
        rows = self.q(f"SELECT diff_type, to_commit, from_commit, {cols} "  # noqa: S608 — names are module constants
                      f"FROM dolt_diff_{table} WHERE to_commit <> 'WORKING'")
        self.manifest["read"][f"{table}_changes"] = len(rows)
        return rows


def read_history(query: Query, *, export: Optional[Sequence[Dict[str, Any]]] = None,
                 source: str = "") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The Dolt history as cairn event objects (unordered against interactions; each carries
    `_order`, the commit index) and the manifest. With `export` (the bd export rows the migration
    imports), the history's last state must BE that export: every memory body and every issue
    column the reader carries, or HistoryError (a stale copy, a gapped log, memories kept
    elsewhere, a dolt that diffs only the working set)."""
    r = Reader(query)
    r.manifest["source"] = source
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
    r.bd_events = []
    actors = r.actors() if "events" in tables else {}
    if "events" in tables:
        unversioned = r.q("SELECT COUNT(*) AS n FROM dolt_diff_events WHERE to_commit = 'WORKING'")
        if unversioned and unversioned[0]["n"] and not r.bd_events:
            r.manifest["warnings"].append(
                f"bd's events table is not versioned in this store (dolt_ignore; bd 1.3 does this): "
                f"its {unversioned[0]['n']} row(s) are in no commit, so no actor comes from it")
    else:
        r.manifest["warnings"].append("no events table: actors come from commit messages only")
    from_messages = 0
    ambiguous = getattr(r, "ambiguous", set())
    for commit, message in r.messages.items():
        if commit in actors or commit in ambiguous:      # two actors in the events table: none
            continue
        said = AUTO_COMMIT_ACTOR.findall(message)
        if len(said) == 1 and message.count("(auto-commit) by") == 1:   # one clear name, or none
            actors[commit] = said[0]
            from_messages += 1
    r.manifest["read"]["actors_from_commit_messages"] = from_messages
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
    last_issue: Dict[str, Optional[Dict[str, Any]]] = {}
    for d in sorted(r.diff("issues", row_cols), key=lambda d: commits[d["to_commit"]][0]):
        commit, kind = d["to_commit"], d["diff_type"]
        if kind in ("added", "modified"):
            last_issue[d["to_id"]] = {c: column_value(c, d.get(f"to_{c}")) for c in diff_cols}
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
            last_issue[issue_id] = None
            notes_seen.discard((issue_id, "notes"))     # a re-created id starts a new chain
            row = {c: column_value(c, d.get(f"from_{c}")) for c in row_cols}
            emit(commit, "deleted", {"issue": row, "labels": [], "comments": [],
                                     "dependencies": []}, issue_id=issue_id, key=("deleted",))
            r.manifest["left_behind"].append(
                f"deleted issue {issue_id}: the text of its comments (its label and dependency "
                "removals are carried as their own events)")

    # memories (and a count of the other config keys, which cairn has no place for)
    other_config = 0
    found_keys: set = set()
    last_memory: Dict[str, Optional[str]] = {}
    for d in sorted(r.diff("config", ("key", "value")), key=lambda d: commits[d["to_commit"]][0]):
        name = d.get("to_key") or d.get("from_key") or ""
        if not name.startswith(MEMORY_PREFIX):
            other_config += 1
            continue
        key = name[len(MEMORY_PREFIX):]
        found_keys.add(key)
        commit, kind = d["to_commit"], d["diff_type"]
        old, new = d.get("from_value"), d.get("to_value")
        last_memory[key] = None if kind == "removed" else new
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
    if export is not None:
        _check_last_state(export, last_issue, last_memory, diff_cols)

    # labels, dependencies, comments
    if "labels" in tables:
        for d in r.diff("labels", ("issue_id", "label")):
            added = d["diff_type"] == "added"
            if d["diff_type"] not in ("added", "removed"):
                r.manifest["dropped"][f"label_{d['diff_type']}"] += 1
                continue
            side = "to" if added else "from"
            commit = d["to_commit"]
            emit(commit, "label_added" if added else "label_removed",
                 {"label": d[f"{side}_label"]}, issue_id=d[f"{side}_issue_id"],
                 key=(d[f"{side}_label"],))
    for d in dep_rows:
        if d["diff_type"] not in ("added", "removed"):
            r.manifest["dropped"][f"dependency_{d['diff_type']}"] += 1
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
    # bd's events table, row by row: what became of each
    disposition: Dict[str, Counter] = defaultdict(Counter)
    for e in r.bd_events:
        kind = e.get("to_event_type") or e.get("from_event_type") or "?"
        if e["diff_type"] == "added":
            how = ("not imported; its commit's changes are carried" if per_commit[e["to_commit"]]
                   else "not imported; its commit changed nothing this reader carries")
        else:
            how = f"{e['diff_type']} from bd's events table (not carried)"
        disposition[kind][how] += 1
    m["bd_events"] = {k: dict(v) for k, v in sorted(disposition.items())}
    m["max_events_in_one_commit"] = max(per_commit.values(), default=0)
    m["events_without_actor"] = sum(1 for e in out if not e["actor"])
    m["left_behind"][:0] = [
        "the Dolt commit graph: hashes and dates ride on the events (data.dolt_commit), parent "
        "links and commit messages stay in Dolt",
        "the order of changes inside one commit (events of one commit are in table, then "
        "column order)",
        f"the Dolt committer ({', '.join(m['committers'])}): never copied into actor",
        *(["bd's events table as rows: read for the actor of each commit only"]
          if "events" in tables else []),
        "changes made between two commits (only committed states exist)",
    ]
    return out, m


def _check_last_state(export: Sequence[Dict[str, Any]],
                      last_issue: Dict[str, Optional[Dict[str, Any]]],
                      last_memory: Dict[str, Optional[str]], columns: Sequence[str]) -> None:
    """The history must end where the export is: else the copy is stale, the log has gaps, or
    this bd keeps something where the reader does not look. Refuse, naming the first few."""
    wrong: List[str] = []
    seen_issues, seen_memories = set(), set()
    for row in export:
        if row.get("_type") == "memory":
            key = row.get("key")
            seen_memories.add(key)
            if key not in last_memory:
                wrong.append(f"memory {key}: no Dolt history under config.{MEMORY_PREFIX}*")
            elif last_memory[key] != row.get("value"):
                wrong.append(f"memory {key}: the last body in Dolt differs from the export")
        elif row.get("_type", "issue") == "issue":
            issue_id = row.get("id")
            seen_issues.add(issue_id)
            state = last_issue.get(issue_id)
            if state is None:
                wrong.append(f"issue {issue_id}: no Dolt history")
                continue
            for col in columns:          # bd's export omits an empty field: absent is empty
                if column_value(col, _export_value(col, row.get(col))) != state[col]:
                    wrong.append(f"issue {issue_id}: {col} in Dolt differs from the export")
                    break
    wrong += [f"issue {i}: in Dolt, not in the export" for i, state in sorted(last_issue.items())
              if state is not None and i not in seen_issues]
    wrong += [f"memory {k}: in Dolt, not in the export" for k, body in sorted(last_memory.items())
              if body is not None and k not in seen_memories]
    if wrong:
        raise HistoryError(f"the Dolt history does not end at the export ({len(wrong)} "
                           f"difference(s); first: {'; '.join(wrong[:3])}). Use the export and "
                           "the Dolt store of the same bd, taken together.")


def _export_value(column: str, value: Any) -> Any:
    """An export value in the shape column_value expects (bd exports times as RFC 3339)."""
    if column in DATETIME_COLUMNS and isinstance(value, str):
        stamp = timeutil.parse(value)
        return stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp else value
    return value


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
        if e["kind"] == "field_change":
            when = timeutil.parse(e["created_at"])
            d = e["data"]

            match = next((i for t, o, n, i in index.get((e["issue_id"], d["field"]), ())
                          if i not in used and when is not None and abs(t - when) <= SAME_CHANGE
                          and _same_change(d, o, n)), None)
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


def _same_change(data: Dict[str, Any], old: Optional[str], new: Optional[str]) -> bool:
    """Whether a Dolt field_change `data` is the change a log row records as old -> new (both
    as `comparable` text). The log has no append shape: a Dolt append matches old + tail."""
    if "appended" in data:
        return old is not None and new == old + data["appended"]
    return old == comparable(data.get("old_value")) and new == comparable(data.get("new_value"))


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
