"""Read queries. The semantics here are bd 1.1.2's, measured on a throwaway bd
store (2026-09-30) rather than assumed:

  blocked   a non-closed issue with a `blocks` dependency on any issue that is
            not closed (an in_progress or deferred blocker still blocks); or,
            failing that, an issue whose parent is blocked — transitively.
            `blocked_by` lists the direct blockers, or the blocked parent when
            there are none.
  ready     status is exactly `open`, not blocked, own defer_until not in the
            future, and no DIRECT parent with a future defer_until (a grandchild
            of a deferred issue is ready; bd does not look further up). The
            parent's own status is not consulted: a CLOSED parent that still
            carries a future defer_until hides its children too.
            A status of `deferred` never appears, even once its date has passed.
  order     priority ascending, then created_at descending (to the second),
            then id ascending.
  `related` and `discovered-from` dependencies never block.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import timeutil

BLOCKING_TYPE = "blocks"
PARENT_TYPE = "parent-child"
COUNTED_TABLES = ("comments", "events", "memories", "memory_edges")

# Key order of an issue object, as bd writes it.
ISSUE_KEY_ORDER = (
    "_type", "id", "title", "description", "design", "acceptance_criteria", "notes",
    "status", "priority", "issue_type", "assignee", "owner", "created_at",
    "created_by", "updated_at", "started_at", "closed_at", "close_reason",
    "comments", "defer_until", "labels", "dependencies", "parent",
    "dependency_count", "dependent_count", "comment_count",
)
# Key order of an exported issue: the field order of bd's Issue struct (bd 1.1.2
# internal/types/types.go), which encoding/json follows; the counts come last.
# Keys cairn carries opaquely (in `extra`) land in their bd position too.
EXPORT_KEY_ORDER = (
    "_type", "id", "title", "description", "design", "acceptance_criteria", "notes",
    "spec_id", "status", "priority", "issue_type", "assignee", "owner",
    "estimated_minutes", "created_at", "created_by", "updated_at", "started_at",
    "closed_at", "close_reason", "closed_by_session", "due_at", "defer_until",
    "external_ref", "source_system", "metadata", "compaction_level", "compacted_at",
    "compacted_at_commit", "original_size", "labels", "dependencies", "comments",
    "sender", "ephemeral", "no_history", "wisp_type", "pinned", "is_template",
    "bonded_from", "await_type", "await_id", "timeout", "waiters", "source_formula",
    "source_location", "mol_type", "work_type", "event_kind", "actor", "target", "payload",
    "dependency_count", "dependent_count", "comment_count",
)
# Keys bd leaves out of an issue nested under `dependencies` in `show`, and out
# of the objects printed by `blocked`.
LINKED_OMITS = ("dependencies", "parent", "dependency_count", "dependent_count",
                "comment_count")
TEXT_COLUMNS = (
    "title", "description", "design", "acceptance_criteria", "notes", "status",
    "issue_type", "assignee", "owner", "created_at", "created_by", "updated_at",
    "started_at", "closed_at", "close_reason", "defer_until",
)


# bd keys a dependency row by a UUIDv5 of (issue, target) and lists an issue's
# dependencies in that key's order (bd 1.1.2 internal/storage/depid; measured on
# every multi-dependency issue in the workspace store). Sorting the same way makes
# cairn's export byte-identical to bd's, so the tracked JSONL does not churn.
BD_DEPID_NAMESPACE = uuid.UUID("bdd74eb9-a20a-554a-985b-54b8f3e64d8b")


def bd_dependency_id(issue_id: str, target: str) -> str:
    return str(uuid.uuid5(BD_DEPID_NAMESPACE, issue_id + "\x1f" + target))


# Dependency order inside an exported issue, by name; meta `dep_order` picks one.
DEP_ORDERS = {
    "edge-id": bd_dependency_id,                       # bd 1.1.x
    "target": lambda issue_id, target: target,         # bd 1.3.x
}
DEFAULT_DEP_ORDER = "edge-id"


def export_lines(conn: sqlite3.Connection, include_memories: bool) -> List[str]:
    """The export, one JSON line per row: issues, then memories."""
    snap = Snapshot(conn)
    snap.deps_of.sort_all()
    lines = [go_json(snap.issue_object(row["id"], shape="export"))
             for row in sorted(snap.issues.values(), key=export_order)]
    if include_memories:
        lines += [go_json({"_type": "memory", "key": r["key"], "value": r["value"]})
                  for r in memories(conn)]
    return lines


EVENT_KEYS = ("id", "kind", "issue_id", "memory_key", "actor", "created_at", "data")


def event_objects(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The history in the order it was recorded, as the objects `export --events`
    writes: every key always present (null when empty), `data` as the object it
    holds. Recording order is SQLite's rowid: the table is append-only (triggers)
    and cairn never VACUUMs, so it is the order of the transactions that wrote the
    rows, and `init --events` inserts in file order. Timestamps alone would not
    do: they have one-second resolution, and ids are random, so a new event of the
    same second could land among older ones. This way two exports of one store are
    equal and a new event is always the last line (git sees an append)."""
    rows = conn.execute("SELECT id, kind, issue_id, memory_key, actor, created_at, data "
                        "FROM events ORDER BY rowid").fetchall()
    return [event_object(r) for r in rows]


def event_object(row: Any) -> Dict[str, Any]:
    """One events row as `export --events` writes it."""
    obj: Dict[str, Any] = {"_type": "event"}
    for key in EVENT_KEYS:
        obj[key] = json.loads(row["data"]) if key == "data" else row[key]
    return obj


def events_lines(conn: sqlite3.Connection) -> List[str]:
    """`export --events`: one JSON line per history row, oldest first. Not part of
    bd's export format (bd's has no event rows), so a file of its own."""
    return [go_json(o) for o in event_objects(conn)]


def export_order(row: Any) -> Any:
    """bd's export row order: priority, newest first, then id (measured on bd
    1.1.2: ORDER BY priority, created_at DESC over an id-ordered scan; created_at
    has one-second resolution, so same-second issues fall back to id)."""
    stamp = row["created_at"] or ""
    return (row["priority"], tuple(-ord(c) for c in stamp), row["id"])


_GO_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
               "\u2028": "\\u2028", "\u2029": "\\u2029"}


def go_json(obj: Any) -> str:
    """One compact JSON line as Go's encoding/json writes it: bd escapes <, >, &
    and U+2028/U+2029. The five characters only occur inside strings, so the
    replacement cannot touch JSON syntax."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for char, escaped in _GO_ESCAPES.items():
        text = text.replace(char, escaped)
    return text


class SortedOnRead(dict):
    """issue id -> list, each list sorted by key(issue id, item) the first time `get` or
    `[]` reads it. Iterating items() or values() gives lists in insertion order."""

    def __init__(self, key) -> None:
        super().__init__()
        self._key = key
        self._sorted: Set[str] = set()

    def __getitem__(self, issue_id: str) -> List[Any]:
        items = super().__getitem__(issue_id)
        if issue_id not in self._sorted:
            items.sort(key=functools.partial(self._key, issue_id))
            self._sorted.add(issue_id)
        return items

    def sort_all(self) -> None:
        """For a caller about to read every list (export): one pass, no per-read checks."""
        for issue_id, items in self.items():
            if issue_id not in self._sorted:
                items.sort(key=functools.partial(self._key, issue_id))
        self._sorted.update(self.keys())

    def get(self, issue_id: str, default: Any = None) -> Any:
        return self[issue_id] if issue_id in self else default


def scope_around(conn: sqlite3.Connection, around: Iterable[str]) -> Tuple[Set[str], Set[int]]:
    """The issue ids and dependency rowids a Snapshot(around=...) loads.

    issue_object(x) reads x's row, labels, comments and edges both ways, and for each
    neighbour the same again (a neighbour's own edges are only counted, so its neighbours'
    rows are not needed). blocked() decides x from x's outgoing edges and its blockers'
    rows, then the same for each parent, up the whole parent-child chain."""
    def rows(sql: str, *id_sets: Set[str]) -> List[sqlite3.Row]:
        return conn.execute(sql, [json.dumps(sorted(ids)) for ids in id_sets]).fetchall()
    within = "IN (SELECT value FROM json_each(?{}))".format
    focus = set(around)
    chain, frontier = set(focus), set(focus)
    while frontier:                           # focus and every ancestor
        frontier = {r["depends_on_id"] for r in rows(
            f"SELECT depends_on_id FROM dependencies WHERE type = '{PARENT_TYPE}' "  # noqa: S608 — constants only, ids bound
            f"AND issue_id {within(1)}", frontier)} - chain
        chain |= frontier
    near = rows(f"SELECT issue_id, depends_on_id FROM dependencies "  # noqa: S608 — constants only, ids bound
                f"WHERE issue_id {within(1)} OR depends_on_id {within(1)}", focus)
    linked = focus | {r["issue_id"] for r in near} | {r["depends_on_id"] for r in near}
    edges = rows(f"SELECT rowid, issue_id, depends_on_id FROM dependencies "  # noqa: S608 — constants only, ids bound
                 f"WHERE issue_id {within(1)} OR depends_on_id {within(2)}",
                 chain | linked, linked)
    ids = chain | linked | {r["depends_on_id"] for r in edges if r["issue_id"] in chain}
    return ids, {r["rowid"] for r in edges}


class Snapshot:
    """Everything the read verbs need, loaded once. The store is small (hundreds
    of issues), so computing in Python is simpler to verify than recursive SQL."""

    def __init__(self, conn: sqlite3.Connection, around: Optional[Iterable[str]] = None) -> None:
        """around: load only what issue_object and blocked() read for these issues (their
        rows, their neighbours' rows and edges, the parent chain above them), not the whole
        store (workspace-7m8.25). Every answer about one of them is the full snapshot's; an
        answer about any other issue is not, so read only these through it."""
        if around is None:
            issues_sql, deps_sql, scoped_sql, params = (
                "SELECT * FROM issues", "SELECT * FROM dependencies", "", ())
        else:
            ids, edges = scope_around(conn, around)
            params = (json.dumps(sorted(ids)),)
            within = "IN (SELECT value FROM json_each(?))"
            issues_sql = f"SELECT * FROM issues WHERE id {within}"  # noqa: S608 — constant fragment, ids bound
            deps_sql = f"SELECT * FROM dependencies WHERE rowid {within} ORDER BY rowid"  # noqa: S608 — constant fragment, ids bound
            scoped_sql = f"WHERE issue_id {within}"
        self.issues: Dict[str, sqlite3.Row] = {
            row["id"]: row for row in conn.execute(issues_sql, params)}
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        # The order bd lists an issue's dependencies in changed between bd versions
        # (1.1.2: by edge id; 1.3.1: by target id). An import records the source's.
        dep_key = DEP_ORDERS.get(meta.get("dep_order") or DEFAULT_DEP_ORDER,
                                 DEP_ORDERS[DEFAULT_DEP_ORDER])
        # By issue only: the order inside one issue's list is bd's (dep_key), and
        # "edge-id" is a uuid5 per edge, the most expensive step of a snapshot at 30k
        # issues (workspace-7m8.24). deps_of and parents_of sort a list when it is read.
        # In rowid order before the sort, as a full scan reads them: the order of
        # dependents_of and of blocked()'s pass over parents_of follows it.
        self.deps: List[sqlite3.Row] = sorted(
            conn.execute(deps_sql, (json.dumps(sorted(edges)),) if around is not None else ()),
            key=lambda d: d["issue_id"])
        learned = meta.get("export_key_order")
        self.export_key_order: Sequence[str] = (
            json.loads(learned) if learned else EXPORT_KEY_ORDER)
        self.labels: Dict[str, List[str]] = {}
        for row in conn.execute(f"SELECT issue_id, label FROM labels {scoped_sql} "  # noqa: S608 — constant fragment, ids bound
                                "ORDER BY issue_id, label", params):
            self.labels.setdefault(row["issue_id"], []).append(row["label"])
        self.comments: Dict[str, List[sqlite3.Row]] = {}
        for row in conn.execute(f"SELECT * FROM comments {scoped_sql} "  # noqa: S608 — constant fragment, ids bound
                                "ORDER BY issue_id, created_at, id", params):
            self.comments.setdefault(row["issue_id"], []).append(row)

        self.deps_of: SortedOnRead = SortedOnRead(
            lambda issue_id, d: dep_key(issue_id, d["depends_on_id"]))
        # Same order as a sort by (issue_id, dep_key): dep_key is one value per
        # (issue, target) pair, so it never reorders the edges into one target.
        self.dependents_of: Dict[str, List[sqlite3.Row]] = {}
        self.parents_of: SortedOnRead = SortedOnRead(dep_key)
        self.children_of: Dict[str, List[str]] = {}
        for dep in self.deps:
            self.deps_of.setdefault(dep["issue_id"], []).append(dep)
            self.dependents_of.setdefault(dep["depends_on_id"], []).append(dep)
            if dep["type"] == PARENT_TYPE:
                self.parents_of.setdefault(dep["issue_id"], []).append(dep["depends_on_id"])
                self.children_of.setdefault(dep["depends_on_id"], []).append(dep["issue_id"])
        self._blocked: Optional[Dict[str, List[str]]] = None

    # -- blocking ---------------------------------------------------------

    def blocked(self) -> Dict[str, List[str]]:
        """Map of blocked issue id -> blocked_by ids."""
        if self._blocked is not None:
            return self._blocked
        blocked: Dict[str, List[str]] = {}
        for dep in self.deps:
            if dep["type"] != BLOCKING_TYPE:
                continue
            issue = self.issues.get(dep["issue_id"])
            blocker = self.issues.get(dep["depends_on_id"])
            if issue is None or blocker is None:
                continue                      # dangling edge: `check` reports it
            if issue["status"] == "closed" or blocker["status"] == "closed":
                continue
            blocked.setdefault(issue["id"], []).append(blocker["id"])
        changed = True
        while changed:                        # a blocked parent blocks its children
            changed = False
            for child, parents in self.parents_of.items():
                if child in blocked or child not in self.issues:
                    continue
                if self.issues[child]["status"] == "closed":
                    continue
                via = [p for p in parents if p in blocked]
                if via:
                    blocked[child] = via
                    changed = True
        # Sorted when one is read: ready and show only test membership (workspace-7m8.24).
        self._blocked = SortedOnRead(lambda issue_id, blocker: blocker)
        self._blocked.update(blocked)
        return self._blocked

    # -- selection --------------------------------------------------------

    def descendants(self, root: str) -> Set[str]:
        found: Set[str] = set()
        stack = [root]
        while stack:
            for child in self.children_of.get(stack.pop(), []):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found

    def ordered(self, ids: Iterable[str]) -> List[str]:
        def key(issue_id: str):
            row = self.issues[issue_id]
            # newest first, at one-second resolution, inside an ascending sort
            return (row["priority"], -timeutil.epoch_seconds(row["created_at"]), issue_id)
        return sorted(ids, key=key)

    def ready(self, *, now: str, include_deferred: bool = False,
              priority: Optional[int] = None, issue_type: Optional[str] = None,
              parent: Optional[str] = None, assignee: Optional[str] = None,
              unassigned: bool = False) -> List[str]:
        blocked = self.blocked()
        scope = self.descendants(parent) if parent else None
        out = []
        for issue_id, row in self.issues.items():
            if row["status"] != "open" or issue_id in blocked:
                continue
            if scope is not None and issue_id not in scope:
                continue
            if priority is not None and row["priority"] != priority:
                continue
            if issue_type is not None and row["issue_type"] != issue_type:
                continue
            if assignee is not None and row["assignee"] != assignee:
                continue
            if unassigned and row["assignee"]:
                continue
            if not include_deferred:
                if timeutil.in_future(row["defer_until"], now):
                    continue
                if any(timeutil.in_future(self.issues[p]["defer_until"], now)
                       for p in self.parents_of.get(issue_id, []) if p in self.issues):
                    continue
            out.append(issue_id)
        return self.ordered(out)

    def select(self, *, statuses: Optional[Sequence[str]] = None,
               include_closed: bool = False, parent: Optional[str] = None,
               issue_type: Optional[str] = None, priority: Optional[int] = None,
               labels: Sequence[str] = ()) -> List[str]:
        out = []
        children = set(self.children_of.get(parent, [])) if parent else None
        for issue_id, row in self.issues.items():
            if statuses:
                if row["status"] not in statuses:
                    continue
            elif not include_closed and row["status"] == "closed":
                continue
            if children is not None and issue_id not in children:
                continue
            if issue_type is not None and row["issue_type"] != issue_type:
                continue
            if priority is not None and row["priority"] != priority:
                continue
            if any(label not in self.labels.get(issue_id, []) for label in labels):
                continue
            out.append(issue_id)
        return self.ordered(out)

    def due_deferred(self, now: str) -> List[str]:
        """Issues parked with status `deferred` whose date has passed. bd never
        brings these back on its own; listing them is a cairn addition."""
        return self.ordered(
            issue_id for issue_id, row in self.issues.items()
            if row["status"] == "deferred" and row["defer_until"]
            and not timeutil.in_future(row["defer_until"], now))

    # -- shaping ----------------------------------------------------------

    def dependency_rows(self, issue_id: str) -> List[Dict[str, Any]]:
        return [{
            "issue_id": dep["issue_id"],
            "depends_on_id": dep["depends_on_id"],
            "type": dep["type"],
            "created_at": dep["created_at"],
            "created_by": dep["created_by"],
            "metadata": dep["metadata"],
        } for dep in self.deps_of.get(issue_id, [])]

    def issue_object(self, issue_id: str, *, shape: str = "list") -> Dict[str, Any]:
        """An issue as bd serialises it. Empty fields are omitted, as bd omits them.

        shape: "export" (adds _type and comments, no parent),
               "list"   (adds parent),
               "show"   (dependencies expanded to issue objects, as bd does; plus
                         `dependents` and `comments`, which bd's show omits).
        """
        row = self.issues[issue_id]
        obj: Dict[str, Any] = {"id": issue_id, "priority": row["priority"]}
        for col in TEXT_COLUMNS:
            if row[col] not in (None, ""):
                obj[col] = row[col]
        if row["extra"]:
            obj.update(json.loads(row["extra"]))
        if self.labels.get(issue_id):
            obj["labels"] = list(self.labels[issue_id])

        deps = self.deps_of.get(issue_id, [])
        if shape == "show":
            if deps:
                obj["dependencies"] = [self._linked(d["depends_on_id"], d["type"]) for d in deps]
            dependents = self.dependents_of.get(issue_id, [])
            if dependents:
                obj["dependents"] = [self._linked(d["issue_id"], d["type"]) for d in dependents]
        elif deps:
            obj["dependencies"] = self.dependency_rows(issue_id)

        comments = self.comments.get(issue_id, [])
        if comments and shape in ("export", "show"):
            obj["comments"] = [{
                "id": c["id"], "issue_id": c["issue_id"], "author": c["author"],
                "text": c["text"], "created_at": c["created_at"],
            } for c in comments]
        if shape != "export":
            parents = self.parents_of.get(issue_id, [])
            if parents:
                obj["parent"] = parents[0]
        if shape == "export":
            obj["_type"] = "issue"

        # Measured on bd 1.1.2: `list`, `ready` and `export` count only `blocks`
        # edges; `show` counts edges of every type (an epic's children included).
        counted = (lambda d: True) if shape == "show" else (lambda d: d["type"] == BLOCKING_TYPE)
        obj["dependency_count"] = sum(1 for d in deps if counted(d))
        obj["dependent_count"] = sum(1 for d in self.dependents_of.get(issue_id, [])
                                     if counted(d))
        obj["comment_count"] = len(comments)
        order = self.export_key_order if shape == "export" else ISSUE_KEY_ORDER
        rank = {name: i for i, name in enumerate(order)}
        return dict(sorted(obj.items(), key=lambda kv: (rank.get(kv[0], len(rank)), kv[0])))

    def _linked(self, issue_id: str, dep_type: str) -> Dict[str, Any]:
        if issue_id not in self.issues:
            return {"id": issue_id, "dependency_type": dep_type, "missing": True}
        out = {k: v for k, v in self.issue_object(issue_id).items()
               if k not in LINKED_OMITS}
        out["dependency_type"] = dep_type
        return out


# -- memories ---------------------------------------------------------------

def memories(conn: sqlite3.Connection, keyword: Optional[str] = None) -> List[sqlite3.Row]:
    rows = list(conn.execute("SELECT * FROM memories ORDER BY key"))
    if keyword:
        needle = keyword.lower()
        rows = [r for r in rows if needle in r["key"].lower() or needle in r["value"].lower()]
    return rows


def recall(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM memories WHERE key = ?", (key,)).fetchone()


# -- health -----------------------------------------------------------------

def blocks_cycles(conn: sqlite3.Connection) -> List[List[str]]:
    """Each set of issues that block one another in a cycle (a strongly connected
    component of the blocks edges with more than one member), members sorted,
    sets sorted. Iterative Tarjan: dependency chains can be deeper than the
    recursion limit. A self edge is reported by check on its own."""
    edges: Dict[str, List[str]] = {}
    for src, dst in conn.execute("SELECT issue_id, depends_on_id FROM dependencies "
                                 "WHERE type = ? ORDER BY issue_id, depends_on_id",
                                 (BLOCKING_TYPE,)):
        edges.setdefault(src, []).append(dst)
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack = set()
    stack: List[str] = []
    found: List[List[str]] = []
    for root in sorted(edges):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = len(index)
                stack.append(node)
                on_stack.add(node)
            succ = edges.get(node, [])
            if i < len(succ):
                work.append((node, i + 1))
                nxt = succ[i]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if low[node] == index[node]:
                members = []
                while True:
                    top = stack.pop()
                    on_stack.discard(top)
                    members.append(top)
                    if top == node:
                        break
                if len(members) > 1:
                    found.append(sorted(members))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return sorted(found)


def check(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """Referential integrity.

    `errors` are hard (dangling or self links, as in bd-memgraph); `warnings`
    want attention but do not fail (a superseded memory that is still stored);
    `notes` are informational.
    """
    errors: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []
    issue_ids = {r["id"] for r in conn.execute("SELECT id FROM issues")}
    keys = {r["key"] for r in conn.execute("SELECT key FROM memories")}
    tombstones = {r["key"]: r["deleted_at"]
                  for r in conn.execute("SELECT key, deleted_at FROM memory_tombstones")}

    for dep in conn.execute("SELECT issue_id, depends_on_id, type FROM dependencies "
                            "ORDER BY issue_id, depends_on_id"):
        if dep["depends_on_id"] not in issue_ids:
            errors.append(f"dangling dependency: {dep['issue_id']} -{dep['type']}-> "
                          f"{dep['depends_on_id']}")
        if dep["depends_on_id"] == dep["issue_id"]:
            errors.append(f"self dependency: {dep['issue_id']}")
    # One parent per issue: `parent` in the JSON is a single id, so a second
    # parent-child edge would be silently dropped from every listing.
    for row in conn.execute(
            "SELECT issue_id, COUNT(*) AS n FROM dependencies WHERE type = ? "
            "GROUP BY issue_id HAVING n > 1 ORDER BY issue_id", (PARENT_TYPE,)):
        errors.append(f"{row['issue_id']} has {row['n']} parents (expected at most one)")
    # dep add refuses a blocks cycle (as bd does); a store written before that,
    # or imported, can still hold one, and its members are missing from ready.
    for members in blocks_cycles(conn):
        errors.append(f"blocks cycle: {', '.join(members)} (none of them can become ready)")

    if "schema_version" in keys:
        warnings.append("memory key 'schema_version' collides with the key bd's JSON "
                        "output reserves; `memories --json` cannot show that memory")

    for edge in conn.execute("SELECT source_key, target, relation, target_kind "
                             "FROM memory_edges ORDER BY source_key, target"):
        source, target = edge["source_key"], edge["target"]
        if edge["target_kind"] == "issue":
            if target not in issue_ids:
                notes.append(f"memory {source} refers to issue {target}, which is not in this store")
            continue
        if target == source:
            errors.append(f"self link: memory {source}")
        elif target not in keys:
            why = (f" (deleted {tombstones[target]})" if target in tombstones else "")
            errors.append(f"dangling link: memory {source} -{edge['relation']}-> {target}{why}")
        elif edge["relation"] == "supersedes":
            warnings.append(f"unswept supersedes: {source} supersedes {target}, which is still stored")
    return {"errors": errors, "warnings": warnings, "notes": notes}


def _count(conn: sqlite3.Connection, table: str) -> int:
    # Table names cannot be bound as parameters, so the name is checked here, not by the caller.
    if table not in COUNTED_TABLES:
        raise ValueError(f"not a counted table: {table!r}")
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 — name checked against COUNTED_TABLES


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    snap = Snapshot(conn)
    by_status: Dict[str, int] = {}
    for row in snap.issues.values():
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    counts = {table: _count(conn, table) for table in COUNTED_TABLES}
    return {
        "issues": len(snap.issues),
        "by_status": dict(sorted(by_status.items())),
        "blocked": len(snap.blocked()),
        "dependencies": len(snap.deps),
        **counts,
    }
