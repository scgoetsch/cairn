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

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from . import timeutil

BLOCKING_TYPE = "blocks"
PARENT_TYPE = "parent-child"

# Key order of an issue object, as bd writes it.
ISSUE_KEY_ORDER = (
    "_type", "id", "title", "description", "design", "acceptance_criteria", "notes",
    "status", "priority", "issue_type", "assignee", "owner", "created_at",
    "created_by", "updated_at", "started_at", "closed_at", "close_reason",
    "comments", "defer_until", "labels", "dependencies", "parent",
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


class Snapshot:
    """Everything the read verbs need, loaded once. The store is small (hundreds
    of issues), so computing in Python is simpler to verify than recursive SQL."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.issues: Dict[str, sqlite3.Row] = {
            row["id"]: row for row in conn.execute("SELECT * FROM issues")}
        self.deps: List[sqlite3.Row] = list(conn.execute(
            "SELECT * FROM dependencies ORDER BY issue_id, depends_on_id, type"))
        self.labels: Dict[str, List[str]] = {}
        for row in conn.execute("SELECT issue_id, label FROM labels ORDER BY issue_id, label"):
            self.labels.setdefault(row["issue_id"], []).append(row["label"])
        self.comments: Dict[str, List[sqlite3.Row]] = {}
        for row in conn.execute("SELECT * FROM comments ORDER BY issue_id, created_at, id"):
            self.comments.setdefault(row["issue_id"], []).append(row)

        self.deps_of: Dict[str, List[sqlite3.Row]] = {}
        self.dependents_of: Dict[str, List[sqlite3.Row]] = {}
        self.parents_of: Dict[str, List[str]] = {}
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
        self._blocked = {k: sorted(v) for k, v in blocked.items()}
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
        rank = {name: i for i, name in enumerate(ISSUE_KEY_ORDER)}
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


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    snap = Snapshot(conn)
    by_status: Dict[str, int] = {}
    for row in snap.issues.values():
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    count = lambda table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return {
        "issues": len(snap.issues),
        "by_status": dict(sorted(by_status.items())),
        "blocked": len(snap.blocked()),
        "dependencies": len(snap.deps),
        "comments": count("comments"),
        "events": count("events"),
        "memories": count("memories"),
        "memory_edges": count("memory_edges"),
    }
