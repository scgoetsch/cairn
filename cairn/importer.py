"""Build a store from a beads export.

Inputs, all plain files:
  - the JSONL written by `bd export --all` (issue rows and memory rows),
  - optionally `.beads/interactions.jsonl` (field-change audit log -> events),
  - optionally the bd-memgraph ledger (timestamps and deletions for memories),
  - optionally a hot list (one memory key per line).

The build is all-or-nothing: the database is written to a temporary file and
renamed over the target only when every row went in. A failed import leaves the
previous database untouched.

Malformed input is an error that names the file and line. Two softer outcomes
are reported instead of raised:
  - `losses`   rows the importer could not represent and left out. A store with
               losses is not a faithful copy.
  - `warnings` oddities in the source data itself (an edge to an issue that is
               gone, a misspelt link type). The copy is still faithful.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from . import links, timeutil
from .store import STATUSES, StoreError, create_db

ISSUE_COLUMNS = (
    "id", "title", "description", "design", "acceptance_criteria", "notes",
    "status", "priority", "issue_type", "assignee", "owner", "created_at",
    "created_by", "updated_at", "started_at", "closed_at", "close_reason",
    "defer_until",
)
REQUIRED_ISSUE_FIELDS = ("id", "title", "status", "priority", "issue_type",
                         "created_at", "updated_at")
TIMESTAMP_FIELDS = ("created_at", "updated_at", "started_at", "closed_at", "defer_until")
# Stored in their own tables, or recomputed on export.
STRUCTURAL_FIELDS = {"_type", "labels", "dependencies", "comments"}
DERIVED_FIELDS = {"dependency_count", "dependent_count", "comment_count"}
# Extra issue-id prefixes (comma-separated) that mark a memory link as an issue
# reference, e.g. a sibling project's prefix. The store's own prefix is detected;
# `issue_prefixes` in the store's config.json is the durable place for the rest.
ENV_PREFIXES = "CAIRN_ISSUE_PREFIXES"


class SourceError(Exception):
    """The source data is malformed. Carries file and line so it can be fixed."""


@dataclass
class ImportReport:
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    losses: List[str] = field(default_factory=list)


def body_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> Iterator[Tuple[int, Dict[str, Any]]]:
    """Yield (line_number, object). A bad line is an error, never a skip."""
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SourceError(f"{path}:{lineno}: not valid JSON ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise SourceError(f"{path}:{lineno}: expected a JSON object")
            yield lineno, obj


def _require_str(row: Mapping[str, Any], name: str, where: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise SourceError(f"{where}: missing or empty '{name}'")
    return value


def _optional_str(row: Mapping[str, Any], name: str, where: str) -> Optional[str]:
    """SQLite would refuse to bind a list or dict, with an error that names
    neither file nor field; check here so the message can."""
    value = row.get(name)
    if value is not None and not isinstance(value, str):
        raise SourceError(
            f"{where}: '{name}' must be a string, got {type(value).__name__}")
    return value


def _insert_issue(conn: sqlite3.Connection, row: Dict[str, Any], where: str) -> None:
    for name in REQUIRED_ISSUE_FIELDS:
        if name not in row or row[name] in (None, ""):
            raise SourceError(f"{where}: issue is missing '{name}'")
    where = f"{where}: issue {row['id']}"
    if row["status"] not in STATUSES:
        raise SourceError(f"{where} has unknown status '{row['status']}' "
                          f"(expected one of {', '.join(STATUSES)})")
    priority = row["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 4:
        raise SourceError(f"{where} has priority {priority!r} (expected an integer 0-4)")
    values: List[Any] = []
    for col in ISSUE_COLUMNS:
        values.append(priority if col == "priority" else _optional_str(row, col, where))
    for col in TIMESTAMP_FIELDS:
        if row.get(col) and timeutil.parse(row[col]) is None:
            raise SourceError(f"{where}: '{col}' is not a timestamp: {row[col]!r}")

    known = set(ISSUE_COLUMNS) | STRUCTURAL_FIELDS | DERIVED_FIELDS
    extra = {k: v for k, v in row.items() if k not in known}
    values.append(json.dumps(extra, sort_keys=True) if extra else None)
    try:
        conn.execute(
            f"INSERT INTO issues ({', '.join(ISSUE_COLUMNS)}, extra) "
            f"VALUES ({', '.join('?' * (len(ISSUE_COLUMNS) + 1))})", values)
    except sqlite3.IntegrityError as exc:
        raise SourceError(f"{where}: {exc}") from exc

    labels = row.get("labels") or []
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise SourceError(f"{where}: 'labels' must be a list of strings")
    for label in labels:
        conn.execute("INSERT OR IGNORE INTO labels(issue_id, label) VALUES (?, ?)",
                     (row["id"], label))

    comments = row.get("comments") or []
    if not isinstance(comments, list) or not all(isinstance(c, dict) for c in comments):
        raise SourceError(f"{where}: 'comments' must be a list of objects")
    for comment in comments:
        spot = f"{where}: comment"
        try:
            conn.execute(
                "INSERT INTO comments(id, issue_id, author, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (_require_str(comment, "id", spot), row["id"],
                 _optional_str(comment, "author", spot),
                 _require_str(comment, "text", spot),
                 _require_str(comment, "created_at", spot)))
        except sqlite3.IntegrityError as exc:
            raise SourceError(f"{spot} {comment.get('id')}: {exc}") from exc


def _insert_dependencies(conn: sqlite3.Connection, row: Dict[str, Any], where: str,
                         issue_ids: Set[str], report: ImportReport) -> int:
    deps = row.get("dependencies") or []
    if not isinstance(deps, list) or not all(isinstance(d, dict) for d in deps):
        raise SourceError(f"{where}: issue {row['id']}: 'dependencies' must be a list of objects")
    count = 0
    for dep in deps:
        spot = f"{where}: dependency of {row['id']}"
        source = _require_str(dep, "issue_id", spot)
        target = _require_str(dep, "depends_on_id", spot)
        dep_type = _require_str(dep, "type", spot)
        if source != row["id"]:
            raise SourceError(f"{spot} belongs to {source}")
        if target not in issue_ids:
            report.warnings.append(
                f"dangling dependency: {source} -{dep_type}-> {target} (target not in the export)")
        try:
            conn.execute(
                "INSERT INTO dependencies(issue_id, depends_on_id, type, created_at, "
                "created_by, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (source, target, dep_type, _optional_str(dep, "created_at", spot),
                 _optional_str(dep, "created_by", spot), _optional_str(dep, "metadata", spot)))
        except sqlite3.IntegrityError as exc:
            raise SourceError(f"{spot}: duplicate edge to {target}: {exc}") from exc
        count += 1
    return count


def _detect_prefix(issue_ids: Set[str]) -> str:
    prefixes = Counter(i.rsplit("-", 1)[0] for i in issue_ids if "-" in i)
    return prefixes.most_common(1)[0][0] if prefixes else ""


def _load_ledger(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceError(f"{path}: not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise SourceError(f"{path}: expected a JSON object of key -> entry")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _load_hot(path: Optional[Path]) -> List[str]:
    if path is None or not path.is_file():
        return []
    seen: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key = line.strip()
        if key and key not in seen:
            seen.append(key)
    return seen


def import_into(conn: sqlite3.Connection, export_path: Path, *,
                interactions_path: Optional[Path] = None,
                ledger_path: Optional[Path] = None,
                hot_path: Optional[Path] = None,
                source_label: str = "",
                extra_prefixes: Sequence[str] = (),
                env: Mapping[str, str] = os.environ) -> ImportReport:
    """Load everything into an empty store. Raises SourceError on bad input."""
    report = ImportReport()
    rows = list(read_jsonl(export_path))

    issue_rows = [(n, r) for n, r in rows if r.get("_type", "issue") == "issue"]
    memory_rows = [(n, r) for n, r in rows if r.get("_type") == "memory"]
    other = Counter(str(r.get("_type")) for _, r in rows
                    if r.get("_type", "issue") not in ("issue", "memory"))
    for kind, count in sorted(other.items()):
        report.losses.append(f"{count} row(s) of unsupported _type '{kind}' were not imported")

    issue_ids: Set[str] = set()
    for lineno, row in issue_rows:
        where = f"{export_path}:{lineno}"
        _require_str(row, "id", where)
        if row["id"] in issue_ids:
            raise SourceError(f"{where}: duplicate issue id {row['id']}")
        issue_ids.add(row["id"])

    dep_count = 0
    for lineno, row in issue_rows:
        _insert_issue(conn, row, f"{export_path}:{lineno}")
    for lineno, row in issue_rows:
        dep_count += _insert_dependencies(conn, row, f"{export_path}:{lineno}",
                                          issue_ids, report)

    prefix = _detect_prefix(issue_ids)
    prefixes = [prefix, *extra_prefixes,
                *(p.strip() for p in env.get(ENV_PREFIXES, "").split(",") if p.strip())]
    ledger = _load_ledger(ledger_path)
    hot_keys = _load_hot(hot_path)

    memories: Dict[str, str] = {}
    for lineno, row in memory_rows:
        where = f"{export_path}:{lineno}"
        key = _require_str(row, "key", where)
        value = row.get("value")
        if not isinstance(value, str):
            raise SourceError(f"{where}: memory {key} has no string 'value'")
        if key in memories:
            raise SourceError(f"{where}: duplicate memory key {key}")
        memories[key] = value
    for key, value in memories.items():
        entry = ledger.get(key, {})
        created = entry.get("first_seen")
        updated = entry.get("last_changed") or created
        conn.execute(
            "INSERT INTO memories(key, value, body_hash, created_at, updated_at, hot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, value, body_hash(value), created, updated, int(key in hot_keys)))
    for key in hot_keys:
        if key not in memories:
            report.warnings.append(f"hot list names a memory that does not exist: {key}")

    edges, unknown = links.parse_edges(memories, issue_ids=issue_ids, prefixes=prefixes)
    for source, relation, target, kind in edges:
        conn.execute(
            "INSERT INTO memory_edges(source_key, target, relation, target_kind) "
            "VALUES (?, ?, ?, ?)", (source, target, relation, kind))
    for source, written, target in unknown:
        report.warnings.append(
            f"memory {source}: unknown link type '{written}::' on [[{target}]] (stored as related)")

    tombstones = 0
    for key, entry in sorted(ledger.items()):
        if entry.get("deleted_at") and key not in memories:
            conn.execute(
                "INSERT INTO memory_tombstones(key, first_seen, deleted_at) VALUES (?, ?, ?)",
                (key, entry.get("first_seen"), entry.get("deleted_at")))
            tombstones += 1

    events = 0
    if interactions_path is not None and interactions_path.is_file():
        for lineno, row in read_jsonl(interactions_path):
            where = f"{interactions_path}:{lineno}"
            extra = row.get("extra") or {}
            if not isinstance(extra, dict):
                raise SourceError(f"{where}: 'extra' must be an object")
            try:
                # bd's audit log records issue field changes only, so memory_key
                # stays empty for imported events.
                conn.execute(
                    "INSERT INTO events(id, kind, issue_id, memory_key, actor, created_at, data) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?)",
                    (_require_str(row, "id", where), _require_str(row, "kind", where),
                     _optional_str(row, "issue_id", where), _optional_str(row, "actor", where),
                     _require_str(row, "created_at", where),
                     json.dumps(extra, sort_keys=True)))
            except sqlite3.IntegrityError as exc:
                raise SourceError(f"{where}: {exc}") from exc
            events += 1

    for key, value in (("prefix", prefix), ("source", source_label or str(export_path)),
                       ("imported_at", timeutil.now_iso())):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.commit()

    report.counts = {
        "issues": len(issue_rows),
        "dependencies": dep_count,
        "comments": conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
        "labels": conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0],
        "memories": len(memories),
        "memory_edges": len(edges),
        "memory_tombstones": tombstones,
        "events": events,
    }
    return report


def refuse_if_writable(target: Path) -> None:
    """A store made by `cairn init` is state, not a derived copy: replacing it
    would destroy its issues, history and id counters, and leave its WAL files
    paired with a different database. Refuse."""
    if not target.is_file():
        return
    try:
        conn = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'writable'").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return                               # not a cairn store: the old behaviour stands
    if row is not None and row[0] == "1":
        raise StoreError(f"{target} is a writable cairn store (cairn init); refusing to "
                         "replace it with an import. Sync or import into another directory.")


def build_store(target: Path, export_path: Path, **sources: Any) -> ImportReport:
    """Build `target` from scratch, atomically replacing any previous database —
    never a writable one."""
    refuse_if_writable(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # pid alone is not unique: two containers sharing a mount can both be pid 1.
    temp = target.with_name(f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    conn = None
    try:
        conn = create_db(temp)
        report = import_into(conn, export_path, **sources)
        conn.close()
        conn = None
        os.replace(temp, target)
        return report
    finally:
        if conn is not None:
            conn.close()
        for leftover in (temp, temp.with_name(temp.name + "-journal")):
            if leftover.exists():
                leftover.unlink()
