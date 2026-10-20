"""Writes. Only on stores made by `cairn init` (meta `writable`); a shadow rebuilt
from bd is refused by `store.open_writable`.

Every write is one transaction, opened with BEGIN IMMEDIATE so two writers
serialise on the database lock (busy_timeout makes the second one wait) instead
of failing on a lock upgrade. The history row for a change is inserted in the
same transaction as the change: a mutation without its event cannot commit.

Semantics follow bd 1.1.2 where bd was measured (2026-10-15, throwaway store),
with these deliberate differences:
  - `--claim` is refused without an actor (bd falls back to git user.name,
    which collapses every session into one claimant);
  - stored status `blocked` cannot be written (blocked-ness is computed from
    dependencies); bd accepts it;
  - a second parent is refused; bd accepts it;
  - `create --id` with an id that exists or ever existed is refused; bd
    silently REPLACES the issue;
  - `delete` without --force is refused (exit 2); bd prints a preview, exit 0;
  - `delete` does not rewrite mentions of the id in other issues' text; bd does;
  - `remember` requires a key (the session hooks' admission rule);
  - closing a closed issue is a no-op, as in bd, but without bd's exit 0 text.
Measured and matched: a deferred or closed issue is not claimable; `close`
refuses an issue with an open blocker unless forced; `--defer`/`--until` take
an ISO date or timestamp, `+Nd`, `+Nw`, `+Nh` or `tomorrow`.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import admission, links, timeutil
from .importer import body_hash
from .queries import BLOCKING_TYPE, PARENT_TYPE, Snapshot
from .store import STATUSES, get_meta

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
MIN_WIDTH = 3
TRIES_PER_WIDTH = 64
DEP_TYPES = (BLOCKING_TYPE, PARENT_TYPE, "related", "discovered-from")
FIELD_COLUMNS = ("title", "description", "design", "acceptance_criteria", "notes",
                 "priority", "issue_type", "assignee", "status", "defer_until")
DEFAULT_CLOSE_REASON = "Closed"


class WriteRefused(Exception):
    """cairn will not do this (exit 2)."""


class WriteConflict(Exception):
    """Not found, or the state does not allow it (exit 1): a claim held by
    another actor, a closed issue claimed."""


def now_iso() -> str:
    return timeutil.now_iso()


RELATIVE_RE = re.compile(r"^\+(\d+)([hdw])$")


def parse_when(text: str) -> str:
    """A defer date as bd 1.1.2 accepts it (measured): an ISO date or timestamp,
    `+Nh`, `+Nd`, `+Nw`, or `tomorrow` (now + 24 h). Parsed BEFORE a transaction
    opens, so a bad date never leaves half a write behind."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    value = text.strip().lower()
    match = RELATIVE_RE.match(value)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        when = now + {"h": timedelta(hours=n), "d": timedelta(days=n),
                      "w": timedelta(weeks=n)}[unit]
    elif value == "tomorrow":
        when = now + timedelta(days=1)
    else:
        parsed = timeutil.parse(text)
        if parsed is None:
            raise WriteRefused(f"not a date: {text!r} (use YYYY-MM-DD, a timestamp, "
                               "+Nh, +Nd, +Nw or tomorrow)")
        when = parsed
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def uuid7() -> str:
    """A time-ordered UUID (RFC 9562 version 7, sub-millisecond precision in
    rand_a), as bd uses for comment ids. Comments are listed by (created_at, id)
    and created_at has whole seconds, so the id has to keep creation order."""
    ns = time.time_ns()
    ms, sub = divmod(ns, 1_000_000)
    rand_a = sub * 4096 // 1_000_000
    rand_b = secrets.randbits(62)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


class Writer:
    def __init__(self, conn: sqlite3.Connection, *, actor: str, owner: Optional[str] = None,
                 issue_prefixes: Sequence[str] = (), clock=now_iso) -> None:
        if not actor:
            raise WriteRefused("no actor (set BEADS_ACTOR or pass --actor)")
        self.conn = conn
        self.actor = actor
        self.owner = owner or None
        self.prefix = get_meta(conn, "prefix") or ""
        self.issue_prefixes = [self.prefix, *issue_prefixes]
        self.clock = clock

    # -- plumbing -----------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[str]:
        """BEGIN IMMEDIATE ... COMMIT; yields the transaction's timestamp."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.clock()
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _event(self, now: str, kind: str, data: Dict[str, Any], *,
               issue_id: Optional[str] = None, memory_key: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO events(id, kind, issue_id, memory_key, actor, created_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"ev-{uuid.uuid4().hex}", kind, issue_id, memory_key, self.actor, now,
             json.dumps(data, sort_keys=True)))

    def _issue(self, issue_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise WriteConflict(f'no issue found matching "{issue_id}"')
        return row

    def _set(self, now: str, row: sqlite3.Row, changes: Dict[str, Any],
             reason: Optional[str] = None) -> Dict[str, Any]:
        """Apply column changes; one field_change event per column that changed
        (bd's audit vocabulary). updated_at moves only when something changed."""
        changed = {k: v for k, v in changes.items() if k != "updated_at" and row[k] != v}
        if not changed:
            return {}
        sets = ", ".join(f"{k} = ?" for k in changed)
        self.conn.execute(f"UPDATE issues SET {sets}, updated_at = ? WHERE id = ?",
                          (*changed.values(), now, row["id"]))
        for col, new in changed.items():
            data = {"field": col, "old_value": row[col], "new_value": new}
            if reason is not None and col == "status":
                data["reason"] = reason
            self._event(now, "field_change", data, issue_id=row["id"])
        return changed

    # -- ids ----------------------------------------------------------------

    def _taken(self, issue_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM issues WHERE id = ? UNION ALL SELECT 1 FROM used_ids WHERE id = ?",
            (issue_id, issue_id)).fetchone() is not None

    def _root_width(self) -> int:
        stem = f"{self.prefix}-"
        width = MIN_WIDTH
        for (issue_id,) in self.conn.execute(
                "SELECT id FROM issues UNION SELECT id FROM used_ids"):
            if issue_id.startswith(stem) and "." not in issue_id:
                width = max(width, len(issue_id) - len(stem))
        return width

    def _new_root_id(self) -> str:
        width = self._root_width()
        while True:
            for _ in range(TRIES_PER_WIDTH):
                candidate = f"{self.prefix}-" + "".join(
                    secrets.choice(ALPHABET) for _ in range(width))
                if not self._taken(candidate):
                    return candidate
            width += 1          # this width is (nearly) used up: widen, never reuse

    def _new_child_id(self, parent: str) -> str:
        """parent.N with N one past both the stored counter and every child
        number this store has seen, so a number is never issued twice."""
        pattern = re.compile(re.escape(parent) + r"\.(\d+)$")
        seen = [int(m.group(1)) for (i,) in self.conn.execute(
            "SELECT id FROM issues UNION SELECT id FROM used_ids") if (m := pattern.match(i))]
        row = self.conn.execute("SELECT next_n FROM id_counters WHERE parent_id = ?",
                                (parent,)).fetchone()
        n = max([row["next_n"] if row else 1, *(s + 1 for s in seen)])
        self.conn.execute(
            "INSERT INTO id_counters(parent_id, next_n) VALUES (?, ?) "
            "ON CONFLICT(parent_id) DO UPDATE SET next_n = excluded.next_n", (parent, n + 1))
        return f"{parent}.{n}"

    # -- issues -------------------------------------------------------------

    def create(self, title: str, *, description: Optional[str] = None,
               design: Optional[str] = None, acceptance: Optional[str] = None,
               notes: Optional[str] = None, priority: int = 2, issue_type: str = "task",
               parent: Optional[str] = None, issue_id: Optional[str] = None,
               labels: Sequence[str] = (), assignee: Optional[str] = None,
               defer: Optional[str] = None) -> str:
        defer_until = parse_when(defer) if defer else None
        if not title or not title.strip():
            raise WriteRefused("a title is required")
        if not 0 <= priority <= 4:
            raise WriteRefused(f"invalid priority {priority} (expected 0-4)")
        with self.transaction() as now:
            if issue_id is not None:
                if parent is not None:
                    raise WriteRefused("--id and --parent cannot be combined (as in bd)")
                if not issue_id.startswith(f"{self.prefix}-"):
                    raise WriteRefused(f"id {issue_id} is not under this store's prefix "
                                       f"'{self.prefix}-'")
                if self._taken(issue_id):
                    raise WriteRefused(
                        f"id {issue_id} exists or existed in this store; refused "
                        "(bd would silently replace the issue)")
                new_id = issue_id
            elif parent is not None:
                self._issue(parent)
                new_id = self._new_child_id(parent)
            else:
                new_id = self._new_root_id()
            self.conn.execute(
                "INSERT INTO issues(id, title, description, design, acceptance_criteria, notes, "
                "status, priority, issue_type, assignee, owner, created_at, created_by, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
                (new_id, title, description or None, design or None, acceptance or None,
                 notes or None, priority, issue_type, assignee or None, self.owner, now,
                 self.actor, now))
            self.conn.execute("INSERT INTO used_ids(id, created_at) VALUES (?, ?)", (new_id, now))
            for label in dict.fromkeys(labels):
                self.conn.execute("INSERT INTO labels(issue_id, label) VALUES (?, ?)",
                                  (new_id, label))
            if parent is not None:
                self._insert_dep(now, new_id, parent, PARENT_TYPE)
            self._event(now, "created", {"title": title, "parent": parent},
                        issue_id=new_id)
            if defer_until:
                row = self._issue(new_id)
                changes = self._status_changes(now, row, "deferred")
                changes["defer_until"] = defer_until
                self._set(now, row, changes)
        return new_id

    def update(self, issue_id: str, *, claim: bool = False, title: Optional[str] = None,
               description: Optional[str] = None, design: Optional[str] = None,
               acceptance: Optional[str] = None, notes: Optional[str] = None,
               append_notes: Optional[str] = None, priority: Optional[int] = None,
               issue_type: Optional[str] = None, status: Optional[str] = None,
               assignee: Optional[str] = None, add_labels: Sequence[str] = (),
               remove_labels: Sequence[str] = (), defer: Optional[str] = None) -> None:
        defer_until = parse_when(defer) if defer else None
        if status is not None:
            self._check_status(status)
        if priority is not None and not 0 <= priority <= 4:
            raise WriteRefused(f"invalid priority {priority} (expected 0-4)")
        with self.transaction() as now:
            row = self._issue(issue_id)
            if claim:
                self._claim(now, row)
                row = self._issue(issue_id)
            changes: Dict[str, Any] = {}
            for col, value in (("title", title), ("description", description),
                               ("design", design), ("acceptance_criteria", acceptance),
                               ("priority", priority), ("issue_type", issue_type)):
                if value is not None:
                    changes[col] = value
            if assignee is not None:
                changes["assignee"] = assignee or None
            if notes is not None:
                changes["notes"] = notes or None
            if append_notes is not None:
                base = changes.get("notes", row["notes"])
                changes["notes"] = f"{base}\n{append_notes}" if base else append_notes
            if status is not None:
                changes.update(self._status_changes(now, row, status))
            if defer_until:
                if status not in (None, "deferred"):
                    raise WriteRefused("--defer and --status together: pick one")
                changes.update(self._status_changes(now, row, "deferred"))
                changes["defer_until"] = defer_until
            self._set(now, row, changes)
            for label in dict.fromkeys(add_labels):
                if self.conn.execute("INSERT OR IGNORE INTO labels(issue_id, label) VALUES (?, ?)",
                                     (issue_id, label)).rowcount:
                    self._label_event(now, issue_id, "label_added", label)
            for label in dict.fromkeys(remove_labels):
                if self.conn.execute("DELETE FROM labels WHERE issue_id = ? AND label = ?",
                                     (issue_id, label)).rowcount:
                    self._label_event(now, issue_id, "label_removed", label)

    def _label_event(self, now: str, issue_id: str, kind: str, label: str) -> None:
        self.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, issue_id))
        self._event(now, kind, {"label": label}, issue_id=issue_id)

    @staticmethod
    def _check_status(status: str) -> None:
        if status == "blocked":
            raise WriteRefused("status 'blocked' cannot be set: blocked-ness is computed from "
                               "dependencies — add a blocker with `dep add` instead")
        if status not in STATUSES:
            raise WriteRefused(f"unknown status '{status}' (expected one of "
                               f"{', '.join(s for s in STATUSES if s != 'blocked')})")

    def _status_changes(self, now: str, row: sqlite3.Row, status: str) -> Dict[str, Any]:
        """Columns that move with a status (measured on bd 1.1.2): in_progress
        stamps started_at once; closed stamps closed_at; leaving closed clears
        closed_at and close_reason; leaving deferred clears defer_until."""
        changes: Dict[str, Any] = {"status": status}
        if status == "in_progress" and not row["started_at"]:
            changes["started_at"] = now
        if status == "closed":
            changes["closed_at"] = now
        elif row["status"] == "closed":
            changes.update(closed_at=None, close_reason=None)
        if status != "deferred" and row["status"] == "deferred":
            changes["defer_until"] = None
        return changes

    def _claim(self, now: str, row: sqlite3.Row) -> None:
        """Compare-and-swap on the assignee: succeeds when the issue is neither
        closed nor deferred (bd 1.1.2 refuses both) and is unassigned or already
        this actor's. Never overwrites another actor. Re-claiming one's own
        in-progress issue changes nothing and writes nothing."""
        if row["assignee"] == self.actor and row["status"] == "in_progress":
            return
        cur = self.conn.execute(
            "UPDATE issues SET assignee = ?, status = 'in_progress', "
            "started_at = COALESCE(started_at, ?), updated_at = ? "
            "WHERE id = ? AND status NOT IN ('closed', 'deferred') "
            "AND (assignee IS NULL OR assignee = '' OR assignee = ?)",
            (self.actor, now, now, row["id"], self.actor))
        if cur.rowcount != 1:
            fresh = self._issue(row["id"])
            if fresh["status"] in ("closed", "deferred"):
                raise WriteConflict(f"issue {row['id']} not claimable: status {fresh['status']}")
            raise WriteConflict(f"issue {row['id']} already claimed by {fresh['assignee']}")
        if not row["started_at"]:
            self._event(now, "field_change",
                        {"field": "started_at", "old_value": None, "new_value": now},
                        issue_id=row["id"])
        for col, new in (("assignee", self.actor), ("status", "in_progress")):
            if row[col] != new:
                self._event(now, "field_change",
                            {"field": col, "old_value": row[col], "new_value": new},
                            issue_id=row["id"])

    def claim_ready(self, **filters: Any) -> Optional[str]:
        """`ready --claim`: claim the first issue `ready` with the same filters
        would list, under the same CAS. An issue assigned to someone else is
        skipped, as bd 1.1.2 does (measured)."""
        with self.transaction() as now:
            ids = Snapshot(self.conn).ready(now=now, **filters)
            for issue_id in ids:
                row = self._issue(issue_id)
                if row["assignee"] in (None, "", self.actor):
                    self._claim(now, row)
                    return issue_id
        return None

    def close(self, issue_id: str, reason: Optional[str] = None, *,
              force: bool = False) -> bool:
        """False when the issue was already closed: bd 1.1.2 leaves it untouched
        (measured), reason and all. An open `blocks` dependency refuses the close
        unless forced, as in bd."""
        with self.transaction() as now:
            row = self._issue(issue_id)
            if row["status"] == "closed":
                return False
            if not force:
                open_blockers = [r[0] for r in self.conn.execute(
                    "SELECT d.depends_on_id FROM dependencies d JOIN issues i "
                    "ON i.id = d.depends_on_id WHERE d.issue_id = ? AND d.type = 'blocks' "
                    "AND i.status != 'closed' ORDER BY d.depends_on_id", (issue_id,))]
                if open_blockers:
                    raise WriteConflict(
                        f"cannot close {issue_id}: blocked by open issues "
                        f"[{', '.join(open_blockers)}] (use --force to override)")
            changes = self._status_changes(now, row, "closed")
            changes["close_reason"] = reason or DEFAULT_CLOSE_REASON
            self._set(now, row, changes, reason=changes["close_reason"])
        return True

    def reopen(self, issue_id: str) -> None:
        with self.transaction() as now:
            row = self._issue(issue_id)
            self._set(now, row, self._status_changes(now, row, "open"))

    def defer(self, issue_id: str, until: str) -> None:
        defer_until = parse_when(until)
        with self.transaction() as now:
            row = self._issue(issue_id)
            changes = self._status_changes(now, row, "deferred")
            changes["defer_until"] = defer_until
            self._set(now, row, changes)

    def undefer(self, issue_id: str) -> None:
        with self.transaction() as now:
            row = self._issue(issue_id)
            changes = self._status_changes(now, row, "open") if row["status"] == "deferred" else {}
            changes["defer_until"] = None
            self._set(now, row, changes)

    def note(self, issue_id: str, text: str) -> None:
        self.update(issue_id, append_notes=text)

    def comment(self, issue_id: str, text: str) -> str:
        if not text:
            raise WriteRefused("an empty comment")
        with self.transaction() as now:
            self._issue(issue_id)
            comment_id = uuid7()
            self.conn.execute(
                "INSERT INTO comments(id, issue_id, author, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (comment_id, issue_id, self.actor, text, now))
            self.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, issue_id))
            self._event(now, "comment_added", {"comment_id": comment_id}, issue_id=issue_id)
        return comment_id

    def delete(self, issue_id: str) -> int:
        """Remove an issue and every edge to or from it. Its id stays in used_ids,
        so it is never issued again. Returns the number of edges removed."""
        with self.transaction() as now:
            row = self._issue(issue_id)
            edges = [dict(r) for r in self.conn.execute(
                "SELECT * FROM dependencies WHERE issue_id = ? OR depends_on_id = ?",
                (issue_id, issue_id))]
            labels = [r[0] for r in self.conn.execute(
                "SELECT label FROM labels WHERE issue_id = ? ORDER BY label", (issue_id,))]
            comments = [dict(r) for r in self.conn.execute(
                "SELECT * FROM comments WHERE issue_id = ? ORDER BY created_at, id", (issue_id,))]
            self.conn.execute("DELETE FROM dependencies WHERE issue_id = ? OR depends_on_id = ?",
                              (issue_id, issue_id))
            # Each surviving issue that lost an edge gets its own history row.
            for edge in edges:
                if edge["issue_id"] != issue_id:
                    self._event(now, "dependency_removed",
                                {"depends_on_id": issue_id, "type": edge["type"],
                                 "reason": "target deleted"}, issue_id=edge["issue_id"])
                    self.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?",
                                      (now, edge["issue_id"]))
            self.conn.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
            # The deleted issue's whole record, so the history can say what was lost.
            self._event(now, "deleted", {"issue": dict(row), "labels": labels,
                                         "comments": comments, "dependencies": edges},
                        issue_id=issue_id)
        return len(edges)

    # -- dependencies -------------------------------------------------------

    def _insert_dep(self, now: str, issue_id: str, depends_on: str, dep_type: str) -> None:
        self.conn.execute(
            "INSERT INTO dependencies(issue_id, depends_on_id, type, created_at, created_by, "
            "metadata) VALUES (?, ?, ?, ?, ?, '{}')", (issue_id, depends_on, dep_type, now,
                                                      self.actor))
        self._event(now, "dependency_added", {"depends_on_id": depends_on, "type": dep_type},
                    issue_id=issue_id)

    def dep_add(self, issue_id: str, depends_on: str, dep_type: str = "blocks") -> bool:
        """`dep add A B`: A depends on B. False when the edge already exists."""
        if dep_type not in DEP_TYPES:
            raise WriteRefused(f"unknown dependency type '{dep_type}' "
                               f"(expected one of {', '.join(DEP_TYPES)})")
        if issue_id == depends_on:
            raise WriteRefused(f"{issue_id} cannot depend on itself")
        with self.transaction() as now:
            self._issue(issue_id)
            self._issue(depends_on)
            if self.conn.execute(
                    "SELECT 1 FROM dependencies WHERE issue_id = ? AND depends_on_id = ? "
                    "AND type = ?", (issue_id, depends_on, dep_type)).fetchone():
                return False
            if dep_type == PARENT_TYPE:
                other = self.conn.execute(
                    "SELECT depends_on_id FROM dependencies WHERE issue_id = ? AND type = ?",
                    (issue_id, PARENT_TYPE)).fetchone()
                if other is not None:
                    raise WriteRefused(f"{issue_id} already has a parent ({other[0]}); "
                                       "an issue has one parent")
            if dep_type == BLOCKING_TYPE:
                path = self._blocks_path(depends_on, issue_id)
                if path is not None:
                    raise WriteConflict("adding dependency would create a cycle: "
                                        + " -> ".join([issue_id] + path))
            self._insert_dep(now, issue_id, depends_on, dep_type)
            self.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, issue_id))
        return True

    def _blocks_path(self, start: str, goal: str) -> Optional[List[str]]:
        """The blocks edges from `start` to `goal` (start first, goal last), or
        None. bd walks blocks edges only, whatever the issues' status (measured on
        1.1.2; the refusal itself also on 1.3.0; workspace-7m8.19): a closed issue on
        the path still counts, a parent-child or related edge does not. Neighbours
        are read per node through the primary key, so the walk touches only what
        `start` reaches, not the whole graph."""
        came_from: Dict[str, Optional[str]] = {start: None}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if node == goal:
                path = []
                while node is not None:
                    path.append(node)
                    node = came_from[node]
                return path[::-1]
            for (nxt,) in self.conn.execute(
                    "SELECT depends_on_id FROM dependencies WHERE issue_id = ? AND type = ? "
                    "ORDER BY depends_on_id", (node, BLOCKING_TYPE)):
                if nxt not in came_from:
                    came_from[nxt] = node
                    queue.append(nxt)
        return None

    def dep_remove(self, issue_id: str, depends_on: str) -> int:
        """Remove every edge from A to B, whatever its type (bd's behaviour)."""
        with self.transaction() as now:
            self._issue(issue_id)
            rows = list(self.conn.execute(
                "SELECT type FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
                (issue_id, depends_on)))
            if not rows:
                raise WriteConflict(f"{issue_id} does not depend on {depends_on}")
            self.conn.execute("DELETE FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
                              (issue_id, depends_on))
            for (dep_type,) in rows:
                self._event(now, "dependency_removed",
                            {"depends_on_id": depends_on, "type": dep_type}, issue_id=issue_id)
            self.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, issue_id))
        return len(rows)

    # -- memories -----------------------------------------------------------

    def _rederive_edges(self) -> None:
        """memory_edges is a projection of the bodies: rebuilt whole, in the same
        transaction as the change, by the parser the importer uses. Whole, not
        per key: a new key can change how another body's link resolves."""
        bodies = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM memories")}
        issue_ids = [r[0] for r in self.conn.execute("SELECT id FROM issues")]
        edges, _unknown = links.parse_edges(bodies, issue_ids=issue_ids,
                                            prefixes=self.issue_prefixes)
        self.conn.execute("DELETE FROM memory_edges")
        self.conn.executemany(
            "INSERT INTO memory_edges(source_key, target, relation, target_kind) "
            "VALUES (?, ?, ?, ?)", [(s, t, r, k) for s, r, t, k in edges])

    def remember(self, key: str, value: str, *, force: bool = False
                 ) -> Tuple[bool, List[str], Optional[str]]:
        """Set a memory: (True when the key is new, keys worth linking to, the key
        a short body nearly duplicates).

        Admission control first (admission.py): a body of at least
        MIN_WORDS_TO_REFUSE words that is a near-duplicate of ANOTHER key is
        refused unless `force`, since the right write is an update of that key; a
        shorter one is stored and that key returned as advice. A key the body
        supersedes is not a candidate: that is the documented way to replace one.
        A related body that links to no memory comes back with the keys worth
        linking to. Rewriting the same key replaces silently, as bd does; the
        event row is the record of what was there."""
        if not key or not key.strip():
            raise WriteRefused("remember needs --key: keyless memories cannot be found, "
                               "deduplicated or updated")
        if not value:
            raise WriteRefused("an empty memory body")
        with self.transaction() as now:
            bodies = {r["key"]: r["value"] for r in self.conn.execute(
                "SELECT key, value FROM memories")}
            written = links.EDGE_RE.findall(value)
            superseded = {t for rel, t in written if rel == "supersedes"}
            scored = admission.score(value, bodies, exclude={key} | superseded)
            near = scored[0][1] if scored and scored[0][0] >= admission.NEAR_DUPLICATE else None
            short = len(admission.tokens(value)) < admission.MIN_WORDS_TO_REFUSE
            if near is not None and not force and not short:
                other = near
                raise WriteConflict(
                    f"near-duplicate of memory '{other}' (overlap {scored[0][0]:.2f}): update "
                    f"it in place (cairn recall {other}, then cairn remember --key {other} "
                    f"\"<merged>\"), or write supersedes::[[{other}]] to replace it; "
                    "--force stores it under this key anyway")
            related: List[str] = []
            if near is None and not any(t in bodies for _, t in written):
                related = [k for s, k in scored[:3] if s >= admission.RELATED]
            old = self.conn.execute("SELECT * FROM memories WHERE key = ?", (key,)).fetchone()
            if old is None:
                self.conn.execute(
                    "INSERT INTO memories(key, value, body_hash, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)", (key, value, body_hash(value), now, now))
                self.conn.execute("DELETE FROM memory_tombstones WHERE key = ?", (key,))
            elif old["value"] != value:
                self.conn.execute(
                    "UPDATE memories SET value = ?, body_hash = ?, updated_at = ? WHERE key = ?",
                    (value, body_hash(value), now, key))
            self._event(now, "memory_set",
                        {"old_hash": old["body_hash"] if old else None,
                         "new_hash": body_hash(value)}, memory_key=key)
            self._rederive_edges()
        return old is None, related, (near if short and not force else None)

    def forget(self, key: str) -> str:
        with self.transaction() as now:
            old = self.conn.execute("SELECT * FROM memories WHERE key = ?", (key,)).fetchone()
            if old is None:
                raise WriteConflict(f'no memory with key "{key}"')
            self.conn.execute("DELETE FROM memories WHERE key = ?", (key,))
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_tombstones(key, first_seen, deleted_at) "
                "VALUES (?, ?, ?)", (key, old["created_at"], now))
            self._event(now, "memory_deleted", {"old_hash": old["body_hash"]}, memory_key=key)
            self._rederive_edges()
        return old["value"]
