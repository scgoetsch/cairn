"""Typed links inside memory bodies.

The notation is the one bd-memgraph established, kept so existing bodies parse
unchanged:

    [[key]]                 related            (also written related::[[key]])
    supersedes::[[key]]     (also depends-on, contradicts, justified-by, refines)
    [[proj-abc]]            a reference to an ISSUE, not a memory

A target of one or two characters, or one of PROSE_TARGETS, is the documented
escape for writing about the notation itself and is not a link.

A target names an issue when it is an issue id in the store; otherwise a memory
when it is a memory key; otherwise an issue reference when it has the shape of
an issue id under one of the store's prefixes (so a link to an issue that was
deleted is still recognised as an issue link); otherwise a memory.
"""

from __future__ import annotations

import re
from typing import Collection, Dict, Iterable, List, Set, Tuple

DEFAULT_RELATION = "related"
EDGE_TYPES = ("supersedes", "depends-on", "contradicts", "justified-by", "refines",
              DEFAULT_RELATION)
EDGE_RE = re.compile(r"(?:([a-z][a-z-]*)::)?\[\[([A-Za-z0-9][A-Za-z0-9._-]*)\]\]")
PROSE_TARGETS = {"key", "wikilink", "wikilinks"}

Edge = Tuple[str, str, str, str]          # (source_key, relation, target, target_kind)
UnknownType = Tuple[str, str, str]        # (source_key, written_type, target)


def issue_ref_re(prefixes: Iterable[str]) -> "re.Pattern[str]":
    """Match the shape of an issue id under `prefixes`: proj-7m8, proj-7m8.2.
    With no prefixes nothing matches."""
    names = sorted({p.lower() for p in prefixes if p})
    if not names:
        return re.compile(r"(?!)")
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?i)^(?:{alternation})-[a-z0-9]+(?:\.[0-9]+)*$")


def parse_edges(memories: Dict[str, str], *, issue_ids: Collection[str] = (),
                prefixes: Iterable[str] = ()) -> Tuple[List[Edge], List[UnknownType]]:
    """Extract links from memory bodies.

    Returns (edges, unknown). An unknown typed link (`frobs::[[x]]`) is kept as a
    `related` edge and also reported, so a typo in a relation name is visible
    rather than silently dropped.
    """
    looks_like_issue = issue_ref_re(prefixes)
    known_issues = set(issue_ids)
    edges: List[Edge] = []
    unknown: List[UnknownType] = []
    seen: Set[Tuple[str, str, str]] = set()
    for source in sorted(memories):
        for match in EDGE_RE.finditer(memories[source]):
            written, target = match.group(1), match.group(2)
            if len(target) <= 2 or target in PROSE_TARGETS:
                continue
            relation = written or DEFAULT_RELATION
            if written and written not in EDGE_TYPES:
                unknown.append((source, written, target))
                relation = DEFAULT_RELATION
            if target in known_issues:
                kind = "issue"
            elif target in memories:
                kind = "memory"
            else:
                kind = "issue" if looks_like_issue.match(target) else "memory"
            triple = (source, relation, target)
            if triple not in seen:
                seen.add(triple)
                edges.append((source, relation, target, kind))
    return edges, unknown
