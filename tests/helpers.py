"""Shared fixtures. Every test builds a real SQLite store in a temp directory
from a small JSONL export — no mocks, and no bd needed."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cairn import cli
from cairn.importer import build_store
from cairn.store import STORE_DIRNAME, db_path, open_db

NOW = "2026-09-30T12:00:00Z"
PAST = "2026-09-01T00:00:00Z"
FUTURE = "2026-10-30T00:00:00Z"


def issue(issue_id: str, **fields: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "_type": "issue", "id": issue_id, "title": f"title of {issue_id}",
        "status": "open", "priority": 2, "issue_type": "task",
        "created_at": "2026-09-10T10:00:00Z", "updated_at": "2026-09-10T10:00:00Z",
    }
    row.update(fields)
    return row


def dep(source: str, target: str, dep_type: str = "blocks") -> Dict[str, Any]:
    return {"issue_id": source, "depends_on_id": target, "type": dep_type,
            "created_at": "2026-09-10T10:00:00Z", "created_by": "tester", "metadata": "{}"}


def with_deps(row: Dict[str, Any], *deps: Dict[str, Any]) -> Dict[str, Any]:
    row["dependencies"] = list(deps)
    return row


def child(issue_id: str, parent: str, **fields: Any) -> Dict[str, Any]:
    return with_deps(issue(issue_id, **fields), dep(issue_id, parent, "parent-child"))


def memory(key: str, value: str) -> Dict[str, Any]:
    return {"_type": "memory", "key": key, "value": value}


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class StoreCase(unittest.TestCase):
    """Gives each test a temp workspace and helpers to build a store in it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.store_dir = self.workspace / STORE_DIRNAME

    def build(self, rows: List[Dict[str, Any]], **sources: Any):
        export = write_jsonl(self.workspace / "export.jsonl", rows)
        report = build_store(db_path(self.store_dir), export, **sources)
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        return conn, report

    def cli(self, *argv: str, cwd: Optional[Path] = None) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["-C", str(cwd or self.workspace), *argv])
            except SystemExit as exc:          # argparse usage errors
                code = exc.code if isinstance(exc.code, int) else 2
        return code, out.getvalue(), err.getvalue()
