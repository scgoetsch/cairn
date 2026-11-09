"""`cairn search`: ranked full-text search over issues and memories.

BM25 over SQLite's FTS5, built in memory from the store for each call (a few
hundred rows index in milliseconds, and nothing has to be kept in step with
the writer). Any query word may match (OR): this is for finding related
earlier work, where recall matters more than precision.

Measured on the workspace store with dev/search-eval.py (workspace-d3ct,
2026-11-09). Finding the earlier issues a new one links to (126 issues), the
top 10 held 79% of them against 62% for newest-first; 65% with notes left out,
which is the floor, since notes written later can carry the newer issue's words.
Finding the memories an issue names (30 issues), 75% against 19% for the hot
tier and 72% for substring matching. Embeddings (nomic-embed-text) did no better,
and worse on issues, so there are none here.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Sequence

KINDS = ("issue", "memory")
# Words too common to rank by. The match itself drops nothing else: an id such
# as proj-7m8 is two tokens to FTS5 and still finds its issue.
STOPWORDS = frozenset(
    "the a an of to in on for and or is are be with by from at as it this that not no into than "
    "then was were has have had but if its can".split())
# Column weights for bm25(): a word in the title counts more than one in a long
# notes field (dev/search-eval.py: 3/1/0.5 and 1/1/1 measured
# within 0.02 of each other; notes left out lost 0.14 of R@10).
WEIGHTS = {"title": 3.0, "body": 1.0, "notes": 0.5}


class SearchUnavailable(RuntimeError):
    """This Python's SQLite was built without FTS5."""


def query_terms(text: str) -> List[str]:
    """The words of `text` that take part in ranking, in order, without repeats."""
    seen: Dict[str, None] = {}
    for word in re.findall(r"[^\W_]+", text.lower()):
        if len(word) > 1 and word not in STOPWORDS:
            seen.setdefault(word, None)
    return list(seen)


def _index() -> sqlite3.Connection:
    idx = sqlite3.connect(":memory:")
    try:
        idx.execute("CREATE VIRTUAL TABLE docs USING fts5(kind UNINDEXED, ref UNINDEXED, "
                    "title, body, notes, tokenize = 'unicode61 remove_diacritics 2')")
    except sqlite3.OperationalError as exc:
        idx.close()
        raise SearchUnavailable(f"this Python's SQLite has no FTS5 ({exc})") from None
    return idx


def search(conn: sqlite3.Connection, text: str, *, kinds: Sequence[str] = KINDS,
           include_closed: bool = True, limit: int = 10) -> List[Dict[str, Any]]:
    """Rows best first: kind, id (issue id or memory key), title, status (issues),
    score (higher is better) and a snippet around the matched words."""
    terms = query_terms(text)
    if not terms:
        return []
    idx = _index()
    try:
        if "issue" in kinds:
            sql = ("SELECT id, title, status, description, design, acceptance_criteria, notes "
                   "FROM issues" + ("" if include_closed else " WHERE status != 'closed'"))
            for r in conn.execute(sql):
                body = "\n".join(t for t in (r["description"], r["design"],
                                             r["acceptance_criteria"]) if t)
                idx.execute("INSERT INTO docs VALUES ('issue', ?, ?, ?, ?)",
                            (r["id"], r["title"], body, r["notes"] or ""))
        if "memory" in kinds:
            for r in conn.execute("SELECT key, value FROM memories"):
                # The key's words count as its title: keys are written as phrases.
                idx.execute("INSERT INTO docs VALUES ('memory', ?, ?, ?, '')",
                            (r["key"], r["key"].replace("-", " "), r["value"]))
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        w = WEIGHTS
        rows = idx.execute(
            "SELECT kind, ref, title, bm25(docs, 0, 0, ?, ?, ?) AS rank, "
            "snippet(docs, -1, '[', ']', '…', 12) AS snip "
            "FROM docs WHERE docs MATCH ? ORDER BY rank, ref LIMIT ?",
            (w["title"], w["body"], w["notes"], match, limit if limit > 0 else -1)).fetchall()
    finally:
        idx.close()
    status = {}
    if any(r[0] == "issue" for r in rows):
        status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM issues")}
    return [{"kind": kind, "id": ref, "title": title if kind == "issue" else None,
             "status": status.get(ref) if kind == "issue" else None,
             "score": round(-rank, 4), "snippet": " ".join(snip.split())}
            for kind, ref, title, rank, snip in rows]
