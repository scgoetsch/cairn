"""`cairn check --stale`: memories whose anchors changed after they were written.

A memory is a claim about the world as it was when it was last written. When
something it names has changed since, the claim may no longer hold (the idea
behind Zep/Graphiti's validity intervals, without an LLM in the loop). Two
anchors are checked, both mechanical:

  issue  an issue the body names (a [[link]] or a bare id) was closed more than
         GRACE after the memory last changed;
  path   a repo-relative path the body names does not exist in the checkout,
         and git deleted it more than GRACE after the memory last changed (a path that never
         was in this repo, such as one in another project, is not reported).

These are prompts for review, not errors: a lesson that names the issue it came
from stays true after the issue closes. The exit code of `check` is unchanged.
Measured on the workspace store (workspace-d3ct, 55 memories): 5 issue findings,
of which 3 named work as still pending that had since been done, 1 was arguably
stale and 1 was a provenance reference ("derived ... see <id>"); a path finding
is what caught a memory that went stale the day its repo changed stores.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import timeutil

# A repo-relative path with at least one slash and a file extension. Absolute
# paths, URLs and ~ paths are other machines' or other repos' business.
# A close or a deletion this soon after the memory's write is taken as the same piece of
# work (the memory written, then the issue closed). Measured on the workspace store
# (workspace-d3ct): those gaps were 0 to 1.6 h; the memories that had gone stale, 5.4 h
# to 20 days.
GRACE = timedelta(hours=2)

PATH_RE = re.compile(r"(?<![\w/.~:-])((?:[\w.-]+/)+[\w.-]*\.[A-Za-z0-9]{1,8})(?![\w/])")


def _issue_ids_in(body: str, pattern: "re.Pattern[str]", known: Dict[str, Tuple[str, str]]) -> List[str]:
    return sorted({m.group(0) for m in pattern.finditer(body) if m.group(0) in known})


def _deleted_paths(checkout: Path) -> Optional[Dict[str, datetime]]:
    """Each path git ever deleted in this checkout's history, with the time of
    its latest deletion; None when git cannot answer (not a repo, no git)."""
    try:
        out = subprocess.run(["git", "-C", str(checkout), "log", "--all", "--diff-filter=D",
                              "--name-only", "--format=@%ct"],
                             capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    deleted: Dict[str, datetime] = {}
    when: Optional[datetime] = None
    for line in out.stdout.splitlines():
        if line.startswith("@"):
            when = datetime.fromtimestamp(int(line[1:]), tz=timezone.utc)
        elif line and when is not None and line not in deleted:
            deleted[line] = when           # newest first: the first seen is the latest
    return deleted


def stale_memories(conn: sqlite3.Connection, checkout: Path) -> Dict[str, object]:
    issues = {r["id"]: (r["status"], r["closed_at"])
              for r in conn.execute("SELECT id, status, closed_at FROM issues")}
    prefixes = sorted({i.rsplit("-", 1)[0] for i in issues if "-" in i}, key=len, reverse=True)
    id_re = (re.compile(r"(?<![\w-])(?:" + "|".join(map(re.escape, prefixes))
                        + r")-[a-z0-9]+(?:\.[0-9]+)*(?![\w-])") if prefixes else re.compile(r"(?!)"))
    deleted = _deleted_paths(checkout)
    findings: List[Dict[str, str]] = []
    unchecked = 0
    for m in conn.execute("SELECT key, value, created_at, updated_at FROM memories ORDER BY key"):
        written = timeutil.parse(m["updated_at"] or m["created_at"])
        if written is None:
            unchecked += 1                 # no time to compare against (imported without events)
            continue
        for iid in _issue_ids_in(m["value"], id_re, issues):
            status, closed_at = issues[iid]
            closed = timeutil.parse(closed_at)
            if status == "closed" and closed is not None and closed > written + GRACE:
                findings.append({"key": m["key"], "anchor": "issue", "target": iid,
                                 "changed": closed_at, "written": m["updated_at"] or m["created_at"],
                                 "message": f"memory {m['key']} names {iid}, closed "
                                            f"{closed_at[:10]}, after the memory last changed "
                                            f"({(m['updated_at'] or m['created_at'])[:10]})"})
        if deleted is None:
            continue
        for path in sorted({p.group(1) for p in PATH_RE.finditer(m["value"])}):
            gone = deleted.get(path)
            if gone is not None and gone > written + GRACE and not (checkout / path).exists():
                stamp = gone.strftime("%Y-%m-%d")
                findings.append({"key": m["key"], "anchor": "path", "target": path,
                                 "changed": gone.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "written": m["updated_at"] or m["created_at"],
                                 "message": f"memory {m['key']} names {path}, deleted {stamp}, "
                                            f"after the memory last changed "
                                            f"({(m['updated_at'] or m['created_at'])[:10]})"})
    return {"stale": findings, "unchecked": unchecked, "paths_checked": deleted is not None}
