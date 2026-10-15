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
import re
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from . import links, timeutil
from .queries import DEP_ORDERS, EXPORT_KEY_ORDER, export_lines
from .store import STATUSES, StoreError, create_db, open_writable

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
    # Set by a writable build's self-check: the store's export equals the source
    # byte for byte (False: equal in content, different in layout).
    byte_identical: Optional[bool] = None
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


def detect_dep_order(rows: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The name in queries.DEP_ORDERS that every multi-dependency issue in the
    source is sorted by. bd changed it between versions, so it is read from the
    data, not assumed. With no multi-dependency issue every order fits: the
    first (bd 1.1's) is returned. None when no known order fits."""
    fits = list(DEP_ORDERS)
    for row in rows:
        deps = row.get("dependencies") or []
        if len(deps) < 2 or not all(isinstance(d, dict) for d in deps):
            continue
        pairs = [(str(d.get("issue_id")), str(d.get("depends_on_id"))) for d in deps]
        fits = [name for name in fits
                if [DEP_ORDERS[name](*p) for p in pairs]
                == sorted(DEP_ORDERS[name](*p) for p in pairs)]
    return fits[0] if fits else None


def learn_key_order(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """The key order the source writes issues in. Each row shows only its
    non-empty keys, so each row only says "a comes before b" for neighbours; the
    order is the topological sort of all those constraints, ties broken by bd's
    known order (keys it does not know: by first appearance). Keys the source
    never used are included at their known place. A newer bd's new fields are
    thereby written back where it writes them."""
    # Tie-break rank: a known key's place in bd's order; an unknown key ranks
    # just after the key it first followed, so unconstrained known keys do not
    # jump ahead of it.
    rank: Dict[str, Tuple[int, int, int]] = {
        k: (i, 0, 0) for i, k in enumerate(EXPORT_KEY_ORDER)}
    after: Dict[str, Set[str]] = {k: set() for k in EXPORT_KEY_ORDER}
    indegree: Dict[str, int] = {k: 0 for k in EXPORT_KEY_ORDER}
    seen = 0
    for row in rows:
        keys = list(row)
        for i, key in enumerate(keys):
            if key not in rank:
                seen += 1
                rank[key] = (rank[keys[i - 1]][0] if i else -1, 1, seen)
                after[key], indegree[key] = set(), 0
        for a, b in zip(keys, keys[1:]):
            if b not in after[a]:
                after[a].add(b)
                indegree[b] += 1
    order: List[str] = []
    ready = sorted((k for k, n in indegree.items() if n == 0), key=rank.__getitem__)
    while ready:
        key = ready.pop(0)
        order.append(key)
        for nxt in after[key]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=rank.__getitem__)
    # rows that contradict each other leave a cycle: the rest by known order
    order += sorted((k for k in indegree if k not in order), key=rank.__getitem__)
    return order


def export_content(rows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Rows keyed by (type, id or key), with dependency lists in a fixed order:
    what must survive a migration, independent of layout."""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        obj = dict(row)
        if isinstance(obj.get("dependencies"), list):
            obj["dependencies"] = sorted(obj["dependencies"],
                                         key=lambda d: json.dumps(d, sort_keys=True))
        if isinstance(obj.get("labels"), list):
            obj["labels"] = sorted(obj["labels"])
        out[(str(obj.get("_type", "issue")), str(obj.get("id") or obj.get("key")))] = obj
    return out


def verify_round_trip(conn: sqlite3.Connection, export_path: Path) -> bool:
    """The migration's proof, for any bd version: the new store's export must hold
    exactly what the source held. A difference in content raises (nothing is
    written); a difference only in layout returns False."""
    source_text = export_path.read_text(encoding="utf-8")
    source = [json.loads(line) for line in source_text.splitlines() if line.strip()]
    has_memories = any(r.get("_type") == "memory" for r in source)
    lines = export_lines(conn, has_memories)
    want, got = export_content(source), export_content([json.loads(line) for line in lines])
    problems: List[str] = []
    for key in sorted(set(want) | set(got)):
        a, b = want.get(key), got.get(key)
        if a is None or b is None:
            problems.append(f"{key[0]} {key[1]} only in the {'store' if a is None else 'source'}")
            continue
        for name in sorted(set(a) | set(b)):
            if a.get(name) != b.get(name) and name not in DERIVED_FIELDS:
                problems.append(f"{key[0]} {key[1]}: '{name}' differs")
    if problems:
        raise SourceError(f"the new store would not hold what the source holds "
                          f"({len(problems)} difference(s)): " + "; ".join(problems[:8]))
    return "".join(line + "\n" for line in lines) == source_text


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

    dep_order = detect_dep_order([r for _, r in issue_rows])
    if dep_order is None:
        report.warnings.append("dependency order in the source matches no known bd version; "
                               "the export keeps bd 1.1's order")
    key_order = learn_key_order([r for _, r in issue_rows])
    for key, value in (("prefix", prefix), ("source", source_label or str(export_path)),
                       ("imported_at", timeutil.now_iso()),
                       ("export_key_order", json.dumps(key_order)),
                       *((("dep_order", dep_order),) if dep_order else ())):
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


CHILD_ID = re.compile(r"^(.+)\.(\d+)$")


def load_seed(path: Optional[Path]) -> Tuple[Set[str], Dict[str, int], Optional[str]]:
    """An allocator seed: {"used_ids": [every id bd ever issued, deleted ones
    included], "child_counters": {parent: last child number bd issued}}. No
    export carries deleted ids, so the seed comes from bd's Dolt history
    (`cairn seed-from-dolt`)."""
    if path is None:
        return set(), {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"{path}: not a readable JSON seed ({exc})") from exc
    used = data.get("used_ids") if isinstance(data, dict) else None
    counters = data.get("child_counters") if isinstance(data, dict) else None
    if not isinstance(used, list) or not all(isinstance(i, str) and i for i in used):
        raise SourceError(f"{path}: 'used_ids' must be a list of ids")
    if not isinstance(counters, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
            for k, v in counters.items()):
        raise SourceError(f"{path}: 'child_counters' must map parent ids to numbers >= 0")
    version = data.get("bd_version")
    return set(used), dict(counters), version if isinstance(version, str) else None


def seed_allocator(conn: sqlite3.Connection, prefix: str, used: Set[str],
                   counters: Mapping[str, int]) -> Dict[str, int]:
    """Fill used_ids and id_counters so that no id or child number the source
    ever issued is issued again: used_ids = imported ids + seed ids; a parent's
    next child number is one past the highest of its seed counter and every
    child number seen."""
    now = timeutil.now_iso()
    imported = {row[0]: row[1] for row in conn.execute("SELECT id, created_at FROM issues")}
    foreign = sorted(i for i in used if not i.startswith(f"{prefix}-"))
    if foreign:
        raise SourceError(f"seed names ids outside prefix '{prefix}-': {', '.join(foreign[:5])}")
    for issue_id, created in imported.items():
        conn.execute("INSERT INTO used_ids(id, created_at) VALUES (?, ?)", (issue_id, created))
    seed_only = sorted(used - set(imported))
    for issue_id in seed_only:
        conn.execute("INSERT INTO used_ids(id, created_at) VALUES (?, ?)", (issue_id, now))
    next_n: Dict[str, int] = {p: n + 1 for p, n in counters.items()}
    for issue_id in (*imported, *seed_only):
        match = CHILD_ID.match(issue_id)
        if match:
            parent, n = match.group(1), int(match.group(2))
            next_n[parent] = max(next_n.get(parent, 1), n + 1)
    for parent, n in sorted(next_n.items()):
        conn.execute("INSERT INTO id_counters(parent_id, next_n) VALUES (?, ?)", (parent, n))
    return {"used_ids": len(imported) + len(seed_only), "seed_only_ids": len(seed_only),
            "id_counters": len(next_n)}


def build_writable(target: Path, export_path: Path, prefix: str, *,
                   seed_path: Optional[Path] = None, bd_version: Optional[str] = None,
                   **sources: Any) -> ImportReport:
    """Create a WRITABLE store at `target` from a bd export: the cutover from bd.
    All or nothing, and only when the copy is faithful: an import with losses,
    or issues under another prefix, is refused and nothing is written."""
    if target.exists():
        raise StoreError(f"{target} already exists")
    used, counters, seed_version = load_seed(seed_path)
    bd_version = bd_version or seed_version
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    conn = None
    try:
        conn = create_db(temp)
        report = import_into(conn, export_path, **sources)
        if report.losses:
            raise SourceError("the export cannot be represented faithfully: "
                              + "; ".join(report.losses))
        stray = [r[0] for r in conn.execute("SELECT id FROM issues")
                 if not r[0].startswith(f"{prefix}-")]
        if stray:
            raise SourceError(f"issues outside prefix '{prefix}-': {', '.join(stray[:5])}")
        report.counts.update(seed_allocator(conn, prefix, used, counters))
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         (("writable", "1"), ("allocator", "1"), ("prefix", prefix),
                          ("migrated_from", str(export_path)),
                          *((("bd_version", bd_version),) if bd_version else ())))
        report.byte_identical = verify_round_trip(conn, export_path)
        conn.commit()
        conn.close()
        conn = None
        try:
            os.link(temp, target)    # atomic, and fails rather than replace a store
        except FileExistsError as exc:
            raise StoreError(f"{target} already exists") from exc
        open_writable(target).close()  # WAL on, and the result opens as a writable store
        return report
    finally:
        if conn is not None:
            conn.close()
        for leftover in (temp, temp.with_name(temp.name + "-journal")):
            if leftover.exists():
                leftover.unlink()
