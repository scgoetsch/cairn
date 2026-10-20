"""More than one clone: the tracked exports are the channel, the database a local copy.

A writable store's database is git-ignored. What git carries between clones is the four files
under `.beads/` beside the store (issues.jsonl, memories.jsonl, allocator.json, events.jsonl).
The database is a copy of those files plus the writes not yet exported. Which of the two is
ahead is read from the event history: `export --events` is one line per event in recording
order, so a later export of one store is a string prefix of the new one (EventsExportTest).
Comparing the store's lines with the file's gives one of:

  equal     in sync
  ahead     the store has writes the files do not have yet: the export refresh writes them
  behind    the files have events the store does not (a pull, or another clone's push): the
            store is rebuilt from the files, which hold everything it has (`cairn sync`)
  diverged  neither is a prefix of the other: two clones wrote. Refused; there is no merge.

While the store is behind or diverged every write is refused, so nobody claims work another
clone has finished, and the export refresh refuses to write, so nobody commits a revert of it
(workspace-7m8.21). Nothing is replayed and no rows are merged: the files are the state, the
events are the cursor.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .importer import build_writable
from .queries import event_object, events_lines, go_json
from .store import DB_FILENAME, StoreError, db_path, get_meta, load_config, open_db, open_writable

EXPORTS_DIRNAME = ".beads"
ISSUES, MEMORIES, ALLOCATOR, EVENTS = "issues.jsonl", "memories.jsonl", "allocator.json", "events.jsonl"
BEHIND, DIVERGED, AHEAD, EQUAL, NO_FILE = "behind", "diverged", "ahead", "equal", "no-exports"
EVENT_COLUMNS = "id, kind, issue_id, memory_key, actor, created_at, data"


def exports_dir(store_dir: Path) -> Path:
    """The tracked exports beside the store: `<checkout>/.beads/`."""
    return store_dir.parent / EXPORTS_DIRNAME


def file_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line]


def relation(store: List[str], tracked: List[str]) -> str:
    if store == tracked:
        return EQUAL
    if len(store) < len(tracked) and tracked[:len(store)] == store:
        return BEHIND
    if len(tracked) < len(store) and store[:len(tracked)] == tracked:
        return AHEAD
    return DIVERGED


def _line_at(conn: sqlite3.Connection, index: int) -> Optional[str]:
    row = conn.execute(f"SELECT {EVENT_COLUMNS} FROM events ORDER BY rowid LIMIT 1 OFFSET ?",
                       (index,)).fetchone()
    return None if row is None else go_json(event_object(row))


def quick_state(conn: sqlite3.Connection, store_dir: Path) -> str:
    """The relation, from the line where the two meet only (every verb runs this, so it reads
    one store row, not the history). `sync` runs the full comparison before it changes anything."""
    tracked = file_lines(exports_dir(store_dir) / EVENTS)
    if not tracked:
        return NO_FILE
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if len(tracked) <= count:
        if _line_at(conn, len(tracked) - 1) != tracked[-1]:
            return DIVERGED
        return EQUAL if len(tracked) == count else AHEAD
    if count and _line_at(conn, count - 1) != tracked[count - 1]:
        return DIVERGED
    return BEHIND


def full_state(conn: sqlite3.Connection, store_dir: Path) -> Tuple[str, str]:
    """(relation, what differs), over the whole history."""
    tracked = file_lines(exports_dir(store_dir) / EVENTS)
    if not tracked:
        return NO_FILE, f"no {EXPORTS_DIRNAME}/{EVENTS} beside the store"
    store = events_lines(conn)
    state = relation(store, tracked)
    if state == EQUAL:
        return state, f"{len(store)} events in both"
    if state == AHEAD:
        return state, f"{len(store) - len(tracked)} events not exported yet"
    if state == BEHIND:
        return state, f"{len(tracked) - len(store)} events in the files the store does not have"
    first = next(i for i, (a, b) in enumerate(zip(store + [""], tracked + [""])) if a != b)
    return state, (f"the histories part at event {first + 1}: the store has "
                   f"{len(store) - first} events the files lack, the files {len(tracked) - first} "
                   "the store lacks")


def stale_message(state: str, store_dir: Path) -> str:
    where = exports_dir(store_dir)
    if state == BEHIND:
        return (f"the store is behind {where}/{EVENTS} (another clone's work came in with git): "
                "run `cairn sync` to bring it up to the files")
    return (f"the store and {where}/{EVENTS} have both moved on (two clones wrote): `cairn sync` "
            "shows where they part; there is no merge, so one side's writes must be redone by hand")


def unmerged(store_dir: Path) -> List[str]:
    """Export files git still has in conflict: never rebuilt from."""
    checkout = store_dir.parent
    try:
        out = subprocess.run(["git", "-C", str(checkout), "ls-files", "-u", "--", EXPORTS_DIRNAME],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.split("\t", 1)[-1] for line in out.stdout.splitlines() if "\t" in line})


def resolve_prefix(store_dir: Path) -> str:
    """The issue prefix for a rebuild: the store's own, else config.json's (cairn init writes it
    there), else the one prefix every root id in issues.jsonl carries."""
    path = db_path(store_dir)
    if path.is_file():
        conn = open_db(path)
        try:
            prefix = get_meta(conn, "prefix")
        finally:
            conn.close()
        if prefix:
            return prefix
    prefix = load_config(store_dir).get("prefix")
    if isinstance(prefix, str) and prefix:
        return prefix
    import json
    roots = set()
    for line in file_lines(exports_dir(store_dir) / ISSUES):
        issue_id = json.loads(line).get("id", "")
        if "-" in issue_id:
            roots.add(issue_id.split(".", 1)[0].rsplit("-", 1)[0])
    if len(roots) == 1:
        return roots.pop()
    raise StoreError("cannot tell the issue prefix (no store, none in .cairn/config.json, and "
                     f"issues.jsonl holds {len(roots)}): run `cairn init -p PREFIX --from ...`")


@dataclass
class SyncResult:
    state: str
    detail: str
    rebuilt: bool = False
    backup: Optional[Path] = None


def sync(store_dir: Path, *, hot_path: Optional[Path] = None) -> SyncResult:
    """Bring the store up to the tracked exports when it is behind (or absent). Refuses
    (StoreError) when the histories diverged or an export file is unmerged; leaves an
    equal or ahead store alone."""
    beads = exports_dir(store_dir)
    conflicted = unmerged(store_dir)
    if conflicted:
        raise StoreError(f"git still has {', '.join(conflicted)} in conflict; resolve the merge "
                         "first (across clones, not by regenerating the files from this store)")
    path = db_path(store_dir)
    if path.is_file():
        conn = open_db(path)
        try:
            state, detail = full_state(conn, store_dir)
        finally:
            conn.close()
        if state == DIVERGED:
            raise StoreError(f"{detail}; {stale_message(DIVERGED, store_dir)}")
        if state != BEHIND:
            return SyncResult(state, detail)
    else:
        state, detail = BEHIND, "no store database here yet (a fresh clone)"
    # A store moved from bd without an interactions log has no history until its first cairn
    # write, so no events.jsonl yet: the rows and the allocator are then the whole state.
    if not (beads / ISSUES).is_file():
        raise StoreError(f"nothing to rebuild from: {beads} lacks {ISSUES}")
    prefix = resolve_prefix(store_dir)
    with tempfile.TemporaryDirectory(prefix="cairn-sync-") as tmp:
        source = Path(tmp) / "export.jsonl"
        with source.open("w", encoding="utf-8") as out:
            for name in (ISSUES, MEMORIES):
                for line in file_lines(beads / name):
                    out.write(line + "\n")
        fresh = store_dir / f"{DB_FILENAME}.sync-{os.getpid()}"
        seed = beads / ALLOCATOR
        build_writable(fresh, source, prefix, seed_path=seed if seed.is_file() else None,
                       events_path=beads / EVENTS if (beads / EVENTS).is_file() else None,
                       hot_path=hot_path if hot_path and hot_path.is_file() else None,
                       extra_prefixes=load_config(store_dir).get("issue_prefixes", []))
        try:
            backup = _swap_in(path, fresh)
        finally:
            for leftover in (fresh, Path(f"{fresh}-wal"), Path(f"{fresh}-shm")):
                if leftover.exists():
                    leftover.unlink()
    return SyncResult(state, detail, rebuilt=True, backup=backup)


def _swap_in(path: Path, fresh: Path) -> Optional[Path]:
    """Replace the database at `path` with `fresh`, holding the old store's write lock so no
    write lands in between; the old file (and its -wal) is kept beside it. A writer that was
    already waiting on the old file finds it replaced and refuses (Writer.transaction)."""
    if not path.exists():
        os.link(fresh, path)
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.with_name(f"{path.name}.before-sync-{stamp}")
    conn = open_writable(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] != count:
            raise StoreError("a write landed while the store was being rebuilt; run `cairn sync` again")
        os.replace(path, backup)
        for suffix in ("-wal", "-shm"):
            side = Path(f"{path}{suffix}")
            if side.exists():
                os.replace(side, Path(f"{backup}{suffix}"))
        os.link(fresh, path)
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    open_writable(path).close()
    return backup
