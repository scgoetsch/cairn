"""Stage-3 differential drill: the same writes against a throwaway bd store and a
throwaway writable cairn store, then the two compared field by field.

Opt-in, like test_bd_integration: CAIRN_TEST_WITH_BD=1 and bd on PATH. Ids are
random in both, so issues are matched by title. cairn's deliberate refusals are
asserted as differences, not hidden.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENABLED = os.environ.get("CAIRN_TEST_WITH_BD") == "1" and shutil.which("bd") is not None
REPO = Path(__file__).resolve().parents[1]
# Compared per issue. Timestamps are compared as present/absent: the two stores
# write a second or two apart.
FIELDS = ("status", "priority", "issue_type", "assignee", "description", "design",
          "acceptance_criteria", "notes", "close_reason", "created_by", "owner", "labels")
STAMPS = ("started_at", "closed_at", "defer_until")


class Side:
    """One store. run() returns (exit code, stdout, stderr)."""

    def __init__(self, root: Path, kind: str, prefix: str = "t") -> None:
        self.root, self.kind = root, kind
        self.ids: Dict[str, str] = {}               # title -> id
        sh(root, "git", "init", "-q", ".")
        sh(root, "git", "config", "user.email", "drill@example.com")
        sh(root, "git", "config", "user.name", "drill")
        if kind == "bd":
            sh(root, "bd", "init", "--skip-agents", "--skip-hooks", "--non-interactive",
               "--sandbox", "-p", prefix, "-q")
        else:
            self.run("init", "--prefix", prefix, actor=None, check=True)

    def run(self, *argv: str, actor: Optional[str] = "alice",
            check: bool = False) -> Tuple[int, str, str]:
        env = {k: v for k, v in os.environ.items() if k != "BEADS_ACTOR"}
        if self.kind == "bd":
            cmd = ["bd", *argv, "--sandbox"]
            if actor:
                env["BEADS_ACTOR"] = actor
        else:
            cmd = [sys.executable, "-m", "cairn", *argv]
            env["PYTHONPATH"] = str(REPO)
            if actor:
                cmd += ["--actor", actor]
        proc = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise AssertionError(f"{self.kind} {' '.join(argv)}: {proc.stderr or proc.stdout}")
        return proc.returncode, proc.stdout, proc.stderr

    def new(self, title: str, *argv: str, actor: str = "alice") -> str:
        _, out, _ = self.run("create", "--silent", "--title", title, *argv, actor=actor,
                             check=True)
        self.ids[title] = out.strip()
        return self.ids[title]

    def state(self) -> Dict[str, Any]:
        _, out, _ = self.run("export", "--all", actor=None, check=True)
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        title_of = {r["id"]: r["title"] for r in rows if r.get("_type", "issue") == "issue"}
        issues: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if r.get("_type", "issue") != "issue":
                continue
            obj = {f: r.get(f) for f in FIELDS}
            obj["labels"] = sorted(r.get("labels") or [])
            for f in STAMPS:
                obj[f] = bool(r.get(f))
            obj["defer_until"] = r.get("defer_until")
            obj["deps"] = sorted((title_of.get(d["depends_on_id"], d["depends_on_id"]), d["type"])
                                 for d in r.get("dependencies") or [])
            obj["comments"] = [(c["author"], c["text"]) for c in r.get("comments") or []]
            issues[r["title"]] = obj
        mems = {r["key"]: r["value"] for r in rows if r.get("_type") == "memory"}
        _, out, _ = self.run("ready", "--json", "-n", "0", actor=None, check=True)
        ready = sorted(title_of[i["id"]] for i in json.loads(out))
        _, out, _ = self.run("blocked", "--json", actor=None, check=True)
        blocked = sorted(title_of[i["id"]] for i in json.loads(out))
        return {"issues": issues, "memories": mems, "ready": ready, "blocked": blocked}


def sh(cwd: Path, *argv: str) -> str:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(argv)}: {proc.stderr or proc.stdout}")
    return proc.stdout


@unittest.skipUnless(ENABLED, "set CAIRN_TEST_WITH_BD=1 with bd on PATH")
class DrillTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        (base / "bd").mkdir()
        (base / "cairn").mkdir()
        self.sides = [Side(base / "bd", "bd"), Side(base / "cairn", "cairn")]

    def both(self, *argv: str, actor: Optional[str] = "alice") -> List[Tuple[int, str, str]]:
        return [s.run(*[s.ids.get(a, a) for a in argv], actor=actor) for s in self.sides]

    def same_exit(self, *argv: str, actor: Optional[str] = "alice", want: int = 0) -> None:
        results = self.both(*argv, actor=actor)
        self.assertEqual([r[0] for r in results], [want, want],
                         f"{argv}: bd={results[0]} cairn={results[1]}")

    def test_drill(self) -> None:
        bd, cairn = self.sides
        for s in self.sides:
            s.new("Root", "-d", "the root", "-p", "1")
            s.new("Child", "--parent", s.ids["Root"], "-t", "bug")
            s.new("Blocker")
            s.new("Spare", "-l", "x,y")

        # claim, refused second claimant, takeover
        self.same_exit("update", "Child", "--claim", actor="A")
        results = self.both("update", "Child", "--claim", actor="B")
        self.assertEqual([r[0] for r in results], [1, 1])
        for r in results:
            self.assertIn("already claimed by A", r[2])
        self.same_exit("update", "Child", "--assignee", "B", actor="B")
        self.same_exit("update", "Child", "--claim", actor="B")

        # a blocker hides the issue from ready; closing it brings it back
        self.same_exit("dep", "add", "Root", "Blocker")
        hidden = [s.state() for s in self.sides]
        for st in hidden:
            self.assertNotIn("Root", st["ready"])
            self.assertIn("Root", st["blocked"])
        self.same_exit("close", "Blocker", "--reason", "fixed")
        for st in (s.state() for s in self.sides):
            self.assertIn("Root", st["ready"])

        # fields, notes, comments, labels, close/reopen, defer/undefer
        self.same_exit("update", "Root", "--design", "G", "--acceptance", "AC",
                       "--notes", "N1", "-p", "0")
        self.same_exit("update", "Root", "--append-notes", "N2", "--add-label", "z")
        self.same_exit("note", "Root", "N3")
        self.same_exit("comment", "Root", "a comment")
        self.same_exit("close", "Child")
        self.same_exit("reopen", "Child")
        self.same_exit("close", "Child", "--reason", "done it")
        self.same_exit("defer", "Spare", "--until", "2099-01-01")
        self.same_exit("undefer", "Spare")
        self.same_exit("defer", "Spare", "--until", "2099-06-01")
        self.same_exit("dep", "add", "Spare", "Root", "--type", "related")
        self.same_exit("dep", "remove", "Spare", "Root")

        # memories
        self.same_exit("remember", "--key", "mem-one", "body [[mem-two]]")
        self.same_exit("remember", "--key", "mem-one", "body two [[mem-two]]")
        self.same_exit("remember", "--key", "mem-two", "x")
        self.same_exit("remember", "--key", "mem-gone", "y")
        self.same_exit("forget", "mem-gone")

        self.same_exit("dep", "add", "Spare", "Child", "--type", "parent-child")   # first parent

        states = [s.state() for s in self.sides]
        self.assertEqual(states[0], states[1])

        # deliberate differences: bd accepts, cairn refuses (exit 2), state unchanged
        for argv in (["dep", "add", "Spare", "Root", "--type", "parent-child"],  # second parent
                     ["update", "Root", "--status", "blocked"],
                     ["create", "--id", "Blocker", "--title", "replacement"]):
            for s in self.sides:
                code, _, err = s.run(*[s.ids.get(a, a) for a in argv])
                if s.kind == "bd":
                    self.assertEqual(code, 0, f"bd {argv}: {err}")
                else:
                    self.assertEqual(code, 2, f"cairn {argv}: {err}")
        after = cairn.state()
        self.assertEqual(after["issues"], states[1]["issues"],
                         "a refused write changed cairn's state")
        bd_after = bd.state()
        self.assertEqual(bd_after["issues"]["Root"]["status"], "blocked")
        self.assertNotIn("Blocker", bd_after["issues"])       # replaced by 'replacement'
        self.assertIn("replacement", bd_after["issues"])


@unittest.skipUnless(ENABLED, "set CAIRN_TEST_WITH_BD=1 with bd on PATH")
class RoundTripTest(unittest.TestCase):
    """Rollback after cairn has written: `cairn export --all` imported into a fresh
    bd. Measured 2026-10-15 on bd 1.1.2: every row comes back identical; but
    bd's child counter restarts from the children it sees, so a child number
    cairn deleted is issued again — a rollback must burn those numbers first."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        (base / "bd").mkdir()
        (base / "cairn").mkdir()
        self.base = base
        self.cairn, self.bd = Side(base / "cairn", "cairn"), Side(base / "bd", "bd")

    def test_export_imports_into_bd_unchanged(self) -> None:
        c = self.cairn
        root = c.new("Root", "-d", "desc", "-l", "x,y", "-p", "1")
        kid = c.new("Kid", "--parent", root, "-t", "bug")
        blk = c.new("Blk")
        gone = c.new("Gone", "--parent", root)
        for argv in (("dep", "add", root, blk), ("comment", root, "hello"),
                     ("update", kid, "--claim"), ("close", blk, "--reason", "r"),
                     ("note", root, "n1"), ("defer", root, "--until", "2099-01-01"),
                     ("remember", "--key", "mem-one", "body [[mem-two]]"),
                     ("remember", "--key", "mem-two", "x"), ("delete", gone, "--force")):
            c.run(*argv, check=True)
        export = self.base / "cairn.jsonl"
        c.run("export", "--all", "-o", str(export), actor=None, check=True)
        self.bd.run("import", str(export), actor=None, check=True)
        _, out, _ = self.bd.run("export", "--all", actor=None, check=True)

        def rows(text: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
            parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
            return {(r.get("_type", "issue"), r.get("id") or r.get("key")): r for r in parsed}
        self.assertEqual(rows(out), rows(export.read_text(encoding="utf-8")))

        # The hazard: bd hands out the deleted child's number again; cairn does not.
        _, nxt, _ = self.bd.run("create", "--silent", "--title", "after", "--parent", root,
                                check=True)
        self.assertEqual(nxt.strip(), gone, "bd no longer reissues a deleted child number "
                         "after import: re-measure, and update the rollback note")
        self.assertEqual(c.new("after", "--parent", root), f"{root}.3")


if __name__ == "__main__":
    unittest.main()
