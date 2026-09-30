"""Session priming payload, built to a byte budget.

Hosts truncate what a session-start hook prints — Claude Code shows the first
10,000 bytes of a hook's output in full and only a 2,000-byte preview beyond
that (measured 2026-09-22 with synthetic hooks) — and a truncated payload loses
its tail silently. So the payload is tiered: hot memories in full, every other
memory as a key only, and the whole thing held under a budget that defaults to
that 10,000. When hot bodies do not fit, the largest are moved to the index and
the payload says which — nothing is dropped without a line saying so.
"""

from __future__ import annotations

import sqlite3
from typing import List, Tuple

from . import PROG
from .queries import Snapshot, memories as all_memories
from .store import get_meta

DEFAULT_BUDGET = 10_000
TOP_READY = 5


def _size(parts: List[str]) -> int:
    return len("\n".join(parts).encode("utf-8"))


def _work_section(snap: Snapshot, now: str) -> List[str]:
    ready = snap.ready(now=now)
    in_progress = snap.select(statuses=["in_progress"])
    due = snap.due_deferred(now)
    lines = ["## Work", ""]
    lines.append(f"- In progress: {len(in_progress)}"
                 + ("" if not in_progress else " — " + ", ".join(in_progress)))
    lines.append(f"- Ready: {len(ready)} (`{PROG} ready`)")
    for issue_id in ready[:TOP_READY]:
        row = snap.issues[issue_id]
        lines.append(f"  - {issue_id} P{row['priority']} {row['title']}")
    if due:
        lines.append(f"- Deferred and now due ({len(due)}) — these stay hidden from "
                     f"`ready` until un-deferred: " + ", ".join(due))
    lines.append("")
    return lines


def build(conn: sqlite3.Connection, *, now: str, budget: int = DEFAULT_BUDGET) -> str:
    snap = Snapshot(conn)
    rows = all_memories(conn)
    total = len(rows)
    hot: List[Tuple[str, str]] = [(r["key"], r["value"]) for r in rows if r["hot"]]
    cold: List[str] = [r["key"] for r in rows if not r["hot"]]

    head = [
        f"# {PROG.capitalize()} Workflow Context",
        "",
        f"> Store imported {get_meta(conn, 'imported_at')} from {get_meta(conn, 'source')}. "
        "Shadow mode: bd is the only writer.",
        "",
    ]
    head += _work_section(snap, now)

    def render(full: List[Tuple[str, str]], index: List[str], demoted: List[str]) -> List[str]:
        parts = list(head)
        parts.append(f"## Persistent Memories — HOT tier ({len(full)} in full of {total} total)")
        parts.append("")
        if demoted:
            parts.append(f"> ⚠ {len(demoted)} hot memory(ies) did not fit the {budget}-byte "
                         "budget and are listed in the index instead: " + ", ".join(demoted))
            parts.append("")
        for key, value in full:
            parts += [f"### {key}", value, ""]
        parts.append(f"## Persistent Memories — index ({len(index)} more; "
                     f"full text with `{PROG} recall <key>`)")
        parts.append("")
        parts += [f"- {key}" for key in index]
        return parts

    full, demoted = list(hot), []
    parts = render(full, sorted(cold), demoted)
    while _size(parts) > budget and full:
        largest = max(full, key=lambda kv: len(kv[1].encode("utf-8")))
        full.remove(largest)
        demoted.append(largest[0])
        parts = render(full, sorted(cold + demoted), sorted(demoted))

    if _size(parts) > budget:
        parts.insert(0, f"> ⚠ Payload is {_size(parts)} bytes, over the {budget}-byte "
                        "budget even with no memory bodies — the tail may be truncated.")
    return "\n".join(parts) + "\n"
