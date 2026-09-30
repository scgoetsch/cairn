"""SQLite store: schema, connections, store discovery."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

STORE_DIRNAME = ".cairn"
DB_FILENAME = "cairn.db"
CONFIG_FILENAME = "config.json"
# The store directory is ignored by git except for these two files: the database
# and logs are derived, the configuration is not.
GITIGNORE = "*\n!.gitignore\n!config.json\n"
SCHEMA_VERSION = 1

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
            return store
    return None


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
    if row is None or int(row["value"]) != SCHEMA_VERSION:
        found = None if row is None else row["value"]
        conn.close()
        raise StoreError(
            f"{path} has schema version {found}, this build expects {SCHEMA_VERSION}")
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]
