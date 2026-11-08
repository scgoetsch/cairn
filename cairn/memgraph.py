"""Read verbs over the memory link graph: `memories --rank` and `related`.

Ported from bd-memgraph (github.com/scgoetsch/bd-memgraph, `rank`, `related`, `lineage`;
workspace-7m8.5.4) onto cairn's own tables: the links are `memory_edges` (parsed from the
bodies on every write), and a memory's last change is `memories.updated_at`, which cairn
keeps itself and a rebuild takes from the memory events. bd-memgraph's ledger
(.beads/memgraph-ledger.json) is not read.

rank: PageRank over the undirected graph of memory-to-memory links (damping 0.85, 60
iterations), times a recency factor, times 0.1 for a key some memory `supersedes`. Recency
is 0.5 ** (age / half_life) from the later of the newest YYYY-MM-DD (or YYYY-MM, as the
15th) in the key or body and updated_at; an undated memory scores 0.3. With weight w:
score = pagerank * ((1 - w) + w * recency). Defaults are bd-memgraph's: w 0.5, half-life
90 days.

related: the links of one memory, out and in, one hop or walked --depth hops (what
bd-memgraph's `lineage` printed). Issue links are listed and not walked.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

DATE_RE = re.compile(r"\b(20[23]\d)-(0[1-9]|1[0-2])(?:-([0-3]\d))?\b")
UNDATED_RECENCY = 0.3
SUPERSEDED_FACTOR = 0.1


def latest_date(text: str) -> Optional[datetime.date]:
    """The newest valid date written in the text; a month alone counts as its 15th."""
    best = None
    for year, month, day in DATE_RE.findall(text):
        try:
            found = datetime.date(int(year), int(month), int(day) if day else 15)
        except ValueError:
            continue
        best = found if best is None or found > best else best
    return best


def stamp_date(stamp: Optional[str]) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(stamp[:10]) if stamp else None
    except ValueError:
        return None


def pagerank(adj: Dict[str, Set[str]], damping: float = 0.85,
             iters: int = 60) -> Dict[str, float]:
    n = len(adj) or 1
    pr = {k: 1.0 / n for k in adj}
    for _ in range(iters):
        pr = {k: (1 - damping) / n + damping * sum(pr[j] / len(adj[j]) for j in adj[k] if adj[j])
              for k in adj}
    return pr


def _graph(conn: sqlite3.Connection) -> Tuple[Dict[str, sqlite3.Row], List[sqlite3.Row]]:
    store = {r["key"]: r for r in conn.execute(
        "SELECT key, value, updated_at FROM memories ORDER BY key")}
    edges = list(conn.execute("SELECT source_key, relation, target, target_kind "
                              "FROM memory_edges ORDER BY source_key, target, relation"))
    return store, edges


def rank(conn: sqlite3.Connection, today: datetime.date, *, half_life: float = 90,
         recency_weight: float = 0.5) -> List[Dict[str, Any]]:
    """Every memory, highest score first (ties by key)."""
    store, edges = _graph(conn)
    adj: Dict[str, Set[str]] = {k: set() for k in store}
    for e in edges:
        src, tgt = e["source_key"], e["target"]
        if e["target_kind"] == "memory" and src in adj and tgt in adj and src != tgt:
            adj[src].add(tgt)
            adj[tgt].add(src)
    pr = pagerank(adj)
    superseded = {e["target"] for e in edges
                  if e["relation"] == "supersedes" and e["target_kind"] == "memory"}
    out = []
    for key, row in store.items():
        dates = [d for d in (latest_date(key + " " + row["value"]), stamp_date(row["updated_at"]))
                 if d is not None]
        when = max(dates) if dates else None
        age = max(0, (today - when).days) if when else None
        rec = UNDATED_RECENCY if age is None else 0.5 ** (age / half_life)
        score = pr[key] * ((1 - recency_weight) + recency_weight * rec)
        if key in superseded:
            score *= SUPERSEDED_FACTOR
        out.append({"key": key, "score": score, "degree": len(adj[key]), "age_days": age,
                    "recency": rec, "superseded": key in superseded})
    out.sort(key=lambda r: (-r["score"], r["key"]))
    return out


def related(conn: sqlite3.Connection, key: str, depth: int = 1) -> List[Dict[str, Any]]:
    """The links out of and into `key`, walked `depth` hops (each memory visited once per
    direction), in walk order. Each item: direction (out/in), level (1 = a direct link),
    relation, key (the other end), kind (memory/issue), via (the memory this hop starts at),
    exists (the other end is a live memory or an issue reference)."""
    store, edges = _graph(conn)
    items: List[Dict[str, Any]] = []

    def walk(start: str, direction: str, level: int, seen: Set[str]) -> None:
        if direction == "out":
            hops = [(e["relation"], e["target"], e["target_kind"]) for e in edges
                    if e["source_key"] == start]
        else:
            hops = [(e["relation"], e["source_key"], "memory") for e in edges
                    if e["target"] == start and e["target_kind"] == "memory"]
        for relation, other, kind in hops:
            items.append({"direction": direction, "level": level, "relation": relation,
                          "key": other, "kind": kind, "via": start,
                          "exists": kind == "issue" or other in store})
            if level < depth and kind == "memory" and other in store and other not in seen:
                seen.add(other)
                walk(other, direction, level + 1, seen)

    walk(key, "out", 1, {key})
    walk(key, "in", 1, {key})
    return items
