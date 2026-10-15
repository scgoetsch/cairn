"""Read an allocator seed out of bd's Dolt store.

No bd export carries the ids bd deleted, and bd's child counters live only in
its database, so the seed for a writable store's allocator comes from Dolt:

  used_ids        every id in the history of the `issues` table (deleted ones too)
  child_counters  bd's `child_counters` table (last child number per parent)

The Dolt directory is COPIED to a temporary directory and the copy is queried
with the `dolt` CLI: the live store is never opened, and it may be locked by bd.
bd embeds Dolt and ships no CLI; any dolt release that reads the store's format
works (measured with 2.4.2 on bd 1.1.2's store).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

QUERIES = {
    "used_ids": "SELECT DISTINCT id FROM dolt_history_issues ORDER BY id",
    "current_ids": "SELECT id FROM issues ORDER BY id",
    "child_counters": "SELECT parent_id, last_child FROM child_counters ORDER BY parent_id",
}


class DoltError(Exception):
    """The dolt CLI is missing or a query failed."""


def find_dolt_repo(path: Path) -> Path:
    """Accept the Dolt repository itself, or a `.beads` / `embeddeddolt` directory
    holding exactly one."""
    path = path.resolve()
    if (path / ".dolt").is_dir():
        return path
    for base in (path, path / "embeddeddolt", path / ".beads" / "embeddeddolt"):
        if base.is_dir():
            repos = [p for p in sorted(base.iterdir()) if (p / ".dolt").is_dir()]
            if len(repos) == 1:
                return repos[0]
            if len(repos) > 1:
                raise DoltError(f"{base} holds {len(repos)} Dolt repositories; name one")
    raise DoltError(f"no Dolt repository at {path}")


def _query(dolt: str, repo: Path, sql: str, env: Dict[str, str]) -> List[Dict[str, Any]]:
    proc = subprocess.run([dolt, "sql", "-r", "json", "-q", sql], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise DoltError(f"dolt sql failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    out = proc.stdout.strip()
    return json.loads(out).get("rows", []) if out else []


def read_seed(source: Path, dolt: str = "dolt") -> Dict[str, Any]:
    """Return {"source", "used_ids", "child_counters", "deleted_ids"} from a copy
    of the Dolt store at `source`."""
    if shutil.which(dolt) is None:
        raise DoltError(f"dolt CLI not found ({dolt}); bd ships none: install a dolt "
                        "release and pass --dolt PATH")
    repo = find_dolt_repo(source)
    with tempfile.TemporaryDirectory(prefix="cairn-dolt-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(repo, copy, symlinks=True)
        home = Path(tmp) / "home"         # dolt wants a user config; never touch ~/.dolt
        home.mkdir()
        env = {**os.environ, "HOME": str(home), "DOLT_ROOT_PATH": str(home)}
        for key, value in (("user.name", "cairn"), ("user.email", "cairn@localhost")):
            subprocess.run([dolt, "config", "--global", "--add", key, value], env=env,
                           capture_output=True, check=False)
        rows = {name: _query(dolt, copy, sql, env) for name, sql in QUERIES.items()}
    used = [r["id"] for r in rows["used_ids"]]
    current = [r["id"] for r in rows["current_ids"]]
    missing = sorted(set(current) - set(used))
    if missing:          # the history table must contain the present
        raise DoltError(f"Dolt history lacks current ids: {', '.join(missing[:5])}")
    return {
        "source": str(repo),
        "used_ids": used,
        "child_counters": {r["parent_id"]: int(r["last_child"]) for r in rows["child_counters"]},
        "deleted_ids": sorted(set(used) - set(current)),
    }
