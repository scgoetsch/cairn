"""More than one clone (workspace-7m8.21): the tracked exports carry the work between clones,
`cairn sync` brings a store up to them, and a stale store can neither write nor be exported over
the files. Two real stores in temp directories; "git" is a copy of the four files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from cairn.store import db_path, open_writable
from cairn.writer import WriteConflict, Writer

from .helpers import StoreCase

FILES = ("issues.jsonl", "memories.jsonl", "allocator.json", "events.jsonl")


class SyncCase(StoreCase):
    """Two clones, a and b, and the helpers; no tests of its own."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("BEADS_ACTOR", None)
        self.a, self.b = self.workspace / "a", self.workspace / "b"
        for clone in (self.a, self.b):
            (clone / ".beads").mkdir(parents=True)
        self.ok(self.a, "init", "--prefix", "t")

    def ok(self, clone: Path, *argv: str) -> str:
        code, out, err = self.cli(*argv, cwd=clone)
        self.assertEqual(code, 0, f"{argv}: {err}")
        return out.strip()

    def w(self, clone: Path, *argv: str) -> str:
        return self.ok(clone, *argv, "--actor", "alice")

    def refresh(self, clone: Path) -> None:
        """What tools/refresh-beads-exports.sh writes under cairn."""
        rows = self.ok(clone, "export", "--all").splitlines()
        beads = clone / ".beads"
        (beads / "issues.jsonl").write_text(
            "".join(r + "\n" for r in rows if '"_type":"memory"' not in r), encoding="utf-8")
        (beads / "memories.jsonl").write_text(
            "".join(r + "\n" for r in rows if '"_type":"memory"' in r), encoding="utf-8")
        self.ok(clone, "export", "--allocator", "-o", str(beads / "allocator.json"))
        self.ok(clone, "export", "--events", "-o", str(beads / "events.jsonl"))

    def push(self, src: Path, dst: Path) -> None:
        """git push from one clone and pull into the other: the four files and the config."""
        for name in FILES:
            shutil.copyfile(src / ".beads" / name, dst / ".beads" / name)
        (dst / ".cairn").mkdir(exist_ok=True)
        shutil.copyfile(src / ".cairn" / "config.json", dst / ".cairn" / "config.json")

    def started(self) -> str:
        issue = self.w(self.a, "create", "--silent", "--title", "shared work")
        self.w(self.a, "remember", "--key", "k", "a memory both clones should carry")
        self.refresh(self.a)
        self.push(self.a, self.b)
        return issue


class SyncTest(SyncCase):
    def test_init_records_the_prefix_for_a_fresh_clone(self):
        config = json.loads((self.a / ".cairn" / "config.json").read_text())
        self.assertEqual(config["prefix"], "t")

    def test_a_fresh_clone_is_told_to_sync_and_sync_builds_it(self):
        self.started()
        code, _, err = self.cli("ready", cwd=self.b)
        self.assertNotEqual(code, 0)
        self.assertIn("cairn sync", err)
        self.assertIn("Synced", self.ok(self.b, "sync"))
        self.assertEqual(self.ok(self.a, "export", "--all"), self.ok(self.b, "export", "--all"))
        self.assertEqual(self.ok(self.a, "export", "--events"), self.ok(self.b, "export", "--events"))

    def test_a_fresh_clone_builds_before_any_event_was_written(self):
        # A store moved from bd without an interactions log has no history until its first
        # cairn write: the clone gets issues, memories and the allocator but no events.jsonl.
        self.started()
        (self.b / ".beads" / "events.jsonl").unlink()
        code, _, err = self.cli("ready", cwd=self.b)
        self.assertIn("cairn sync", err)
        self.ok(self.b, "sync")
        self.assertEqual(self.ok(self.a, "export", "--all"), self.ok(self.b, "export", "--all"))

    def test_the_prefix_comes_from_the_issues_when_config_has_none(self):
        self.started()
        (self.b / ".cairn" / "config.json").write_text("{}\n")
        self.ok(self.b, "sync")
        self.assertTrue(self.w(self.b, "create", "--silent", "--title", "x").startswith("t-"))

    def test_behind_refuses_writes_warns_reads_and_sync_catches_up(self):
        issue = self.started()
        self.ok(self.b, "sync")
        self.w(self.a, "close", issue)
        self.refresh(self.a)
        self.push(self.a, self.b)
        code, out, _ = self.cli("sync", "--status", cwd=self.b)
        self.assertEqual((code, out.split(":")[0]), (1, "behind"))
        code, _, err = self.cli("update", issue, "--claim", "--actor", "bob", cwd=self.b)
        self.assertEqual(code, 2)
        self.assertIn("write refused", err)
        self.assertIn("cairn sync", err)
        code, _, err = self.cli("ready", cwd=self.b)
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        self.assertIn("old database is", self.ok(self.b, "sync"))
        self.assertTrue(any(p.name.startswith("cairn.db.before-sync-")
                            for p in (self.b / ".cairn").iterdir()))
        _, out, _ = self.cli("show", issue, "--json", cwd=self.b)
        self.assertEqual(json.loads(out)[0]["status"], "closed")
        self.w(self.b, "create", "--silent", "--title", "after the catch-up")

    def test_ahead_is_left_alone(self):
        self.started()
        self.ok(self.b, "sync")
        self.w(self.b, "create", "--silent", "--title", "local only")
        code, out, _ = self.cli("sync", "--status", cwd=self.b)
        self.assertEqual((code, out.split(":")[0]), (0, "ahead"))
        self.assertIn("Nothing to do", self.ok(self.b, "sync"))

    def test_diverged_refuses_writes_and_sync(self):
        self.started()
        self.ok(self.b, "sync")
        self.w(self.a, "create", "--silent", "--title", "written in a")
        self.w(self.b, "create", "--silent", "--title", "written in b")
        self.refresh(self.a)
        self.push(self.a, self.b)
        code, out, _ = self.cli("sync", "--status", cwd=self.b)
        self.assertEqual((code, out.split(":")[0]), (2, "diverged"))
        code, _, err = self.cli("create", "--title", "more", "--actor", "bob", cwd=self.b)
        self.assertEqual(code, 2)
        self.assertIn("both moved on", err)
        code, _, err = self.cli("sync", cwd=self.b)
        self.assertEqual(code, 2)
        self.assertIn("part at event", err)

    def test_a_write_that_waited_on_the_replaced_file_is_refused(self):
        self.started()
        self.ok(self.b, "sync")
        path = db_path(self.b / ".cairn")
        writer = Writer(open_writable(path), actor="bob", db_file=path)
        self.addCleanup(writer.conn.close)
        os.replace(path, path.with_name("moved.db"))
        shutil.copyfile(path.with_name("moved.db"), path)
        with self.assertRaises(WriteConflict):
            writer.create("late")


class MergeTest(SyncCase):
    """A merge in the checkout beside the store (workspace-7m8.18 re-measure, 2026-10-20): the
    working events.jsonl holds both branches' lines, and that alone must not stop writes when
    the store already has both sides' history (worktrees share one store)."""

    def git(self, clone: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        proc = subprocess.run(["git", "-C", str(clone), *argv], capture_output=True, text=True,
                              env=env)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def commit_exports(self, clone: Path, message: str) -> None:
        self.refresh(clone)
        self.git(clone, "add", ".beads", ".cairn/config.json", ".cairn/.gitignore")
        self.git(clone, "commit", "-q", "-m", message)

    def test_a_merge_over_one_shared_store_leaves_writes_open(self):
        self.git(self.a, "init", "-q", "-b", "main")
        self.w(self.a, "create", "--silent", "--title", "base")
        self.commit_exports(self.a, "base")
        self.git(self.a, "branch", "lane")
        self.w(self.a, "create", "--silent", "--title", "on main")
        self.commit_exports(self.a, "main")
        self.git(self.a, "checkout", "-q", "lane")          # same store: it has "on main" too
        self.w(self.a, "create", "--silent", "--title", "on the lane")
        self.commit_exports(self.a, "lane")
        self.git(self.a, "checkout", "-q", "main")
        merge = self.git(self.a, "merge", "--no-edit", "lane", check=False)
        self.assertNotEqual(merge.returncode, 0, "expected the exports to conflict")
        code, out, _ = self.cli("sync", "--status", cwd=self.a)
        self.assertEqual((code, out.split(":")[0]), (0, "ahead"))
        self.w(self.a, "create", "--silent", "--title", "written during the merge")
        self.assertIn("Nothing to do", self.ok(self.a, "sync"))

    def test_a_merge_of_another_clones_history_stays_refused(self):
        self.git(self.a, "init", "-q", "-b", "main")
        self.w(self.a, "create", "--silent", "--title", "base")
        self.commit_exports(self.a, "base")
        self.git(self.b, "init", "-q", "-b", "main")
        self.git(self.b, "pull", "-q", str(self.a), "main")
        self.ok(self.b, "sync")
        self.w(self.b, "create", "--silent", "--title", "in b")
        self.commit_exports(self.b, "b")
        self.w(self.a, "create", "--silent", "--title", "in a")
        self.commit_exports(self.a, "a")
        merge = self.git(self.a, "pull", "--no-rebase", "--no-edit", str(self.b), "main", check=False)
        self.assertNotEqual(merge.returncode, 0, "expected the exports to conflict")
        code, out, _ = self.cli("sync", "--status", cwd=self.a)
        self.assertEqual((code, out.split(":")[0]), (2, "diverged"))
        code, _, err = self.cli("create", "--title", "more", "--actor", "x", cwd=self.a)
        self.assertEqual(code, 2, err)
