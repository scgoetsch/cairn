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

import json
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


def count_and_last_line(path: Path) -> Optional[Tuple[int, str]]:
    """(lines, last line) of `path` as file_lines counts them, read in chunks plus the tail,
    without decoding the file: every verb asks, and a 30k-issue history is 26 MB, which took
    file_lines 44 ms (workspace-7m8.24). None when the file has a blank line: file_lines
    skips those, so the caller reads it whole."""
    if not path.is_file():
        return 0, ""
    count, prev, size = 0, b"\n", 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            if b"\n\n" in prev[-1:] + chunk:
                return None
            count += chunk.count(b"\n")
            prev, size = chunk, size + len(chunk)
        if size == 0:
            return 0, ""
        if not prev.endswith(b"\n"):
            count += 1                        # a last line without its newline
        tail = b""
        while True:
            start = max(0, size - len(tail) - (1 << 16))
            f.seek(start)
            tail = f.read(size - start)
            body = tail.rstrip(b"\n")
            cut = body.rfind(b"\n")
            if cut >= 0 or start == 0:
                return count, body[cut + 1:].decode("utf-8")


def relation(store: List[str], tracked: List[str]) -> str:
    if store == tracked:
        return EQUAL
    if len(store) < len(tracked) and tracked[:len(store)] == store:
        return BEHIND
    if len(tracked) < len(store) and store[:len(tracked)] == tracked:
        return AHEAD
    return DIVERGED


def _line_at(conn: sqlite3.Connection, index: int) -> Optional[str]:
    row = conn.execute(f"SELECT {EVENT_COLUMNS} FROM events ORDER BY rowid LIMIT 1 OFFSET ?",  # noqa: S608 — EVENT_COLUMNS is a constant, offset bound
                       (index,)).fetchone()
    return None if row is None else go_json(event_object(row))


def merging(store_dir: Path) -> bool:
    """A merge is in progress in the checkout beside the store."""
    try:
        out = subprocess.run(["git", "-C", str(store_dir.parent), "rev-parse", "-q", "--verify",
                              "MERGE_HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def committed_lines(store_dir: Path, ref: str) -> Optional[List[str]]:
    try:
        out = subprocess.run(["git", "-C", str(store_dir.parent), "show",
                              f"{ref}:{EXPORTS_DIRNAME}/{EVENTS}"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return [l for l in out.stdout.split("\n") if l] if out.returncode == 0 else None


def merge_holds_nothing_new(conn: sqlite3.Connection, store_dir: Path) -> bool:
    """Mid-merge, the working events.jsonl holds both branches' lines (conflicted, or joined), a
    prefix of nothing. What matters is what the two sides COMMITTED: when the store already has
    both histories, it is ahead of the merge, as with worktrees that share one store (they
    exported one history, and regenerating the files from the store resolves the conflict). When
    the other side has events the store lacks, another clone wrote: that stays diverged."""
    if not merging(store_dir):
        return False
    store = events_lines(conn)
    for ref in ("HEAD", "MERGE_HEAD"):
        lines = committed_lines(store_dir, ref)
        if lines is None:
            continue
        if store[:len(lines)] != lines:
            return False
    return True


STAMP_FILENAME = "events-sync-stamp.json"   # in .cairn/, which git ignores
STAMP_FIELDS = ("file_dev", "file_ino", "file_size", "file_mtime_ns", "db_dev", "db_ino", "lines")


def _first_line(f) -> str:
    """The first non-empty line of an open binary file, as file_lines yields it ("" when none)."""
    f.seek(0)
    for raw in f:
        line = raw.rstrip(b"\n")
        if line:
            return line.decode("utf-8")
    return ""


def _last_line(f, size: int) -> str:
    """The last non-empty line, read back from the end of an open binary file."""
    tail = b""
    while True:
        start = max(0, size - len(tail) - (1 << 16))
        f.seek(start)
        tail = f.read(size - start)
        body = tail.rstrip(b"\n")
        cut = body.rfind(b"\n")
        if cut >= 0 or start == 0:
            return body[cut + 1:].decode("utf-8")


def _read_stamp(store_dir: Path) -> Optional[dict]:
    """The sidecar, or None for anything that is not a well-formed v1 stamp (a miss, never an
    error: every verb of a writable store reads it)."""
    try:
        stamp = json.loads((store_dir / STAMP_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stamp, dict) or stamp.get("v") != 1:
        return None
    if any(type(stamp.get(k)) is not int for k in STAMP_FIELDS):
        return None
    if stamp["lines"] < 1 or not all(isinstance(stamp.get(k), str) for k in ("first", "last")):
        return None
    return stamp


def _file_stamp(st: os.stat_result) -> Tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _hit(conn: sqlite3.Connection, store_dir: Path, path: Path) -> Optional[str]:
    """`equal` or `ahead` when the sidecar says this exact file was verified a prefix of this
    exact database and nothing since says otherwise; None for a miss."""
    stamp = _read_stamp(store_dir)
    if stamp is None:
        return None
    try:
        db = os.stat(db_path(store_dir))
    except OSError:
        return None
    if (db.st_dev, db.st_ino) != (stamp["db_dev"], stamp["db_ino"]):
        return None
    with open(path, "rb") as f:
        before = os.fstat(f.fileno())
        if _file_stamp(before) != tuple(stamp[k] for k in STAMP_FIELDS[:4]):
            return None
        lines = stamp["lines"]
        if _first_line(f) != stamp["first"] or _last_line(f, before.st_size) != stamp["last"]:
            return None
        if _line_at(conn, 0) != stamp["first"] or _line_at(conn, lines - 1) != stamp["last"]:
            return None
        after = os.fstat(f.fileno())
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            return None
    try:
        now = os.stat(path)
    except OSError:
        return None
    if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino):
        return None                                   # the path was replaced after the open
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if count < lines:
        return None
    return EQUAL if count == lines else AHEAD


def _write_stamp(store_dir: Path, path: Path, before: os.stat_result) -> None:
    """Record that the file at stamp `before` is a prefix of this database. Only when the file
    did not change while it was compared; a failed write leaves "compare every verb"."""
    try:
        lines = file_lines(path)                      # read first, so the restat covers the read
        after = os.stat(path)
        if not lines or not path.is_file() or _file_stamp(after) != _file_stamp(before):
            return
        db = os.stat(db_path(store_dir))
        stamp = dict(zip(STAMP_FIELDS[:4], _file_stamp(after)))
        stamp.update(v=1, db_dev=db.st_dev, db_ino=db.st_ino, lines=len(lines),
                     first=lines[0], last=lines[-1])
    except OSError:
        return
    fd, tmp = tempfile.mkstemp(dir=store_dir, prefix=".stamp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(stamp, f)
        os.replace(tmp, store_dir / STAMP_FILENAME)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def quick_state(conn: sqlite3.Connection, store_dir: Path) -> str:
    """The relation, cheaply. Every verb runs this. The common case is a sidecar hit: this exact
    file was verified a prefix of this exact database, so a count and two rows answer, with no
    decode of the history. Anything else is the full comparison, cached when it says the file
    is a prefix and no merge is under way. Comparing only the line where the two meet, as this
    did before, called an edit of an older line `equal` or `ahead` and let writes through
    (workspace-7m8.4 review; specs/2026-10-22-cairn-gap-closure.md, section 1)."""
    path = exports_dir(store_dir) / EVENTS
    if not path.is_file():
        return full_state(conn, store_dir)[0]         # no-exports: never opened, never cached
    hit = _hit(conn, store_dir, path)
    if hit is not None:
        return hit
    before = os.stat(path)
    state = full_state(conn, store_dir)[0]
    if state in (EQUAL, AHEAD) and not merging(store_dir):
        _write_stamp(store_dir, path, before)
    return state


def full_state(conn: sqlite3.Connection, store_dir: Path) -> Tuple[str, str]:
    """(relation, what differs), over the whole history."""
    tracked = file_lines(exports_dir(store_dir) / EVENTS)
    if not tracked:
        return NO_FILE, f"no {EXPORTS_DIRNAME}/{EVENTS} beside the store"
    store = events_lines(conn)
    state = relation(store, tracked)
    if state in (DIVERGED, BEHIND) and merge_holds_nothing_new(conn, store_dir):
        return AHEAD, "a merge is in progress and the store already holds both sides' history"
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
    if conflicted and db_path(store_dir).is_file():
        conn = open_db(db_path(store_dir))
        try:
            shared = merge_holds_nothing_new(conn, store_dir)
        finally:
            conn.close()
        if shared:
            return SyncResult(AHEAD, "a merge is in progress and the store already holds both "
                              "sides' history: regenerate the exports from it")
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
