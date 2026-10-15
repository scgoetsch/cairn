"""SQLite store: schema, connections, store discovery."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

STORE_DIRNAME = ".cairn"
DB_FILENAME = "cairn.db"
CONFIG_FILENAME = "config.json"
REDIRECT_FILENAME = "redirect"
# The store directory is ignored by git except for these two files: the database
# and logs are derived, the configuration is not.
GITIGNORE = "*\n!.gitignore\n!config.json\n"
SCHEMA_VERSION = 1
# A writer waits this long for another writer's transaction before giving up.
# Inside the shadow run's 15 s deadline and the session hooks' budgets.
BUSY_TIMEOUT_MS = 5000

# Schema migrations: MIGRATIONS[n] holds the statements that take a store from
# version n to n + 1. `cairn migrate` applies them in order, in one transaction,
# after a backup. A shadow is never migrated: `cairn shadow sync` rebuilds it.
MIGRATIONS: Dict[int, Sequence[str]] = {}

# bd's stored statuses. "blocked" is accepted because bd accepts it as a stored
# status; whether an issue IS blocked is computed from dependencies (queries.py).
STATUSES = ("open", "in_progress", "blocked", "deferred", "closed")


class StoreError(Exception):
    """A store could not be found, opened, or is not a cairn store."""


SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE issues (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    description         TEXT,
    design              TEXT,
    acceptance_criteria TEXT,
    notes               TEXT,
    status              TEXT NOT NULL
                        CHECK (status IN ('open','in_progress','blocked','deferred','closed')),
    priority            INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 4),
    issue_type          TEXT NOT NULL,
    assignee            TEXT,
    owner               TEXT,
    created_at          TEXT NOT NULL,
    created_by          TEXT,
    updated_at          TEXT NOT NULL,
    started_at          TEXT,
    closed_at           TEXT,
    close_reason        TEXT,
    defer_until         TEXT,
    -- JSON object of source fields this schema has no column for, so an import
    -- followed by an export loses nothing.
    extra               TEXT
);
CREATE INDEX issues_status ON issues(status);

CREATE TABLE labels (
    issue_id TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    PRIMARY KEY (issue_id, label)
);

-- depends_on_id has no foreign key on purpose: a source store can hold an edge
-- to an issue that no longer exists, and refusing the whole import over it would
-- hide everything else. `check` reports such edges instead.
CREATE TABLE dependencies (
    issue_id      TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    depends_on_id TEXT NOT NULL,
    type          TEXT NOT NULL,
    created_at    TEXT,
    created_by    TEXT,
    metadata      TEXT,
    PRIMARY KEY (issue_id, depends_on_id, type)
);
CREATE INDEX dependencies_target ON dependencies(depends_on_id);

CREATE TABLE comments (
    id         TEXT PRIMARY KEY,
    issue_id   TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    author     TEXT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- History. Append-only: the triggers below make an UPDATE or DELETE fail, so a
-- history row cannot be rewritten or dropped without the statement erroring.
CREATE TABLE events (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    issue_id   TEXT,
    memory_key TEXT,
    actor      TEXT,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX events_issue ON events(issue_id);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE memories (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    body_hash  TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    hot        INTEGER NOT NULL DEFAULT 0 CHECK (hot IN (0, 1))
);

-- Typed links parsed out of memory bodies. target_kind says which namespace the
-- target lives in: another memory, or an issue id.
CREATE TABLE memory_edges (
    source_key  TEXT NOT NULL REFERENCES memories(key) ON DELETE CASCADE,
    target      TEXT NOT NULL,
    relation    TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('memory', 'issue')),
    PRIMARY KEY (source_key, target, relation)
);

-- Keys that once existed and were deleted; lets `check` explain a dangling link.
CREATE TABLE memory_tombstones (
    key        TEXT PRIMARY KEY,
    first_seen TEXT,
    deleted_at TEXT
);

-- Id allocation (writable stores only; an imported shadow leaves both empty).
-- used_ids holds every id ever issued and is never deleted from, so a deleted
-- issue's id is not handed out again. id_counters holds the next child number
-- per parent; numbers are never reused either (bd's rule, measured).
CREATE TABLE used_ids (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE id_counters (
    parent_id TEXT PRIMARY KEY,
    next_n    INTEGER NOT NULL CHECK (next_n >= 1)
);
"""


def find_store_dir(start: Optional[Path] = None,
                   env: Mapping[str, str] = os.environ) -> Optional[Path]:
    """Locate the store directory.

    CAIRN_DIR wins when set. Otherwise walk up from `start` to the filesystem
    root looking for a `.cairn` directory. The walk does NOT stop at a git
    repository boundary: a nested repo inside the workspace still finds the
    workspace store (bd's own discovery stops at that boundary).
    """
    override = env.get("CAIRN_DIR")
    if override:
        return Path(override)
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        store = candidate / STORE_DIRNAME
        if store.is_dir():
            return follow_redirect(store)
    return None


def follow_redirect(store: Path) -> Path:
    """A git worktree checks out the tracked part of `.cairn/` but not the database. Its
    untracked `.cairn/redirect` names the store directory to use instead (one absolute
    path, as bd's `.beads/redirect` does); one hop, never a chain."""
    redirect = store / REDIRECT_FILENAME
    if not redirect.is_file():
        return store
    target = Path(redirect.read_text(encoding="utf-8").strip())
    if not target.is_absolute():
        raise StoreError(f"{redirect} must hold an absolute path, not {str(target)!r}")
    if not target.is_dir():
        raise StoreError(f"{redirect} points at {target}, which is not a directory")
    if (target / REDIRECT_FILENAME).exists():
        raise StoreError(f"{redirect} points at another redirect ({target}); one hop only")
    return target


def db_path(store_dir: Path) -> Path:
    return store_dir / DB_FILENAME


def load_config(store_dir: Path) -> Dict[str, Any]:
    """Read `config.json` from the store directory; {} when there is none.

    Keys:
      issue_prefixes   list of extra issue-id prefixes (besides the store's own)
                       that mark a memory link as an issue reference
    """
    path = store_dir / CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"{path}: not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise StoreError(f"{path}: expected a JSON object")
    prefixes = data.get("issue_prefixes", [])
    if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
        raise StoreError(f"{path}: 'issue_prefixes' must be a list of strings")
    return data


def create_db(path: Path) -> sqlite3.Connection:
    """Create a fresh database file with the schema. Fails if `path` exists."""
    if path.exists():
        raise StoreError(f"{path} already exists")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def open_db(path: Path) -> sqlite3.Connection:
    """Open an existing store read-only. Read verbs never write."""
    if not path.is_file():
        raise StoreError(
            f"no store database at {path} — run `cairn shadow sync` to build it")
    # as_uri() percent-encodes the path; a raw path containing '?', '#' or '%'
    # would be misread as URI syntax.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise StoreError(f"{path} is not a cairn store: {exc}") from exc
    try:
        check_version(path, None if row is None else row["value"])
    except StoreError:
        conn.close()
        raise
    return conn


def init_store(path: Path, prefix: str) -> sqlite3.Connection:
    """Create a WRITABLE store: the schema, plus meta keys `writable` and
    `allocator` that an import never sets. A shadow database rebuilt from bd
    therefore can never be written to by mistake."""
    if not prefix or not all(c.isalnum() or c in "_-" for c in prefix):
        raise StoreError(f"invalid issue prefix {prefix!r}")
    conn = create_db(path)
    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                     (("writable", "1"), ("allocator", "1"), ("prefix", prefix)))
    conn.commit()
    conn.close()
    return open_writable(path)


def open_writable(path: Path) -> sqlite3.Connection:
    """Open a writable store for one write. WAL so read verbs and session hooks
    keep reading while a write runs; busy_timeout so concurrent writers wait
    instead of failing. Autocommit mode: the caller opens BEGIN IMMEDIATE."""
    if not path.is_file():
        raise StoreError(f"no store database at {path} — run `cairn init` to create one")
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        check_version(path, None if row is None else row["value"])
        if get_meta(conn, "writable") != "1":
            raise StoreError(
                f"{path} is a read-only shadow of bd (rebuilt from `bd export` on every sync); "
                "bd is the only writer of this store. Writes go to stores made by `cairn init`.")
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise StoreError(f"{path} is not a cairn store: {exc}") from exc
    except StoreError:
        conn.close()
        raise
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def check_version(path: Path, found: Optional[str]) -> None:
    """Refuse a store of another schema version, saying what fixes it."""
    if found is not None and found.isdigit() and int(found) == SCHEMA_VERSION:
        return
    if found is not None and found.isdigit() and int(found) < SCHEMA_VERSION:
        raise StoreError(
            f"{path} has schema version {found}, this build expects {SCHEMA_VERSION}: "
            "run `cairn migrate` (a writable store) or `cairn shadow sync` (a shadow)")
    raise StoreError(f"{path} has schema version {found}, this build expects "
                     f"{SCHEMA_VERSION}: a newer cairn made it; upgrade cairn")


def migrate(path: Path, *, migrations: Optional[Mapping[int, Sequence[str]]] = None,
            target: int = SCHEMA_VERSION) -> Tuple[int, int, Optional[Path]]:
    """Bring a writable store to schema version `target`. Returns (from, to,
    backup path or None when nothing needed doing).

    A consistent copy of the database goes to `<db>.v<from>.bak` first. Then,
    holding the write lock, every step runs in ONE transaction with foreign keys
    checked before commit: a failed step leaves the store as it was."""
    steps = MIGRATIONS if migrations is None else migrations
    if not path.is_file():
        raise StoreError(f"no store database at {path}")
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = OFF")      # cannot change inside a transaction
        conn.execute("BEGIN IMMEDIATE")
        found = get_meta(conn, "schema_version")
        if found is None or not found.isdigit():
            raise StoreError(f"{path} is not a cairn store (no schema_version)")
        if get_meta(conn, "writable") != "1":
            raise StoreError(f"{path} is a shadow; rebuild it with `cairn shadow sync` "
                             "instead of migrating it")
        start = int(found)
        if start == target:
            conn.execute("ROLLBACK")
            return start, start, None
        if start > target:
            raise StoreError(f"{path} has schema version {start}, newer than this build's "
                             f"{target}: upgrade cairn")
        missing = [v for v in range(start, target) if v not in steps]
        if missing:
            raise StoreError(f"no migration from schema version {missing[0]}")
        backup = path.with_name(f"{path.name}.v{start}.bak")
        if backup.exists():
            raise StoreError(f"{backup} already exists; move it away first")
        # Through a second connection: Python's backup from the connection that
        # holds the write transaction retries forever (measured). Readers are
        # allowed under our lock and no writer is, so the copy is consistent.
        source = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
        dest = sqlite3.connect(str(backup))
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        try:
            for version in range(start, target):
                for statement in steps[version]:
                    conn.execute(statement)
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                         (str(target),))
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise StoreError(f"migration left {len(broken)} broken reference(s); "
                                 "rolled back")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            backup.unlink()          # the store is unchanged: a retry must not be refused
            raise
        conn.execute("COMMIT")
        return start, target, backup
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
