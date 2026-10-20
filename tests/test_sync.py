"""More than one clone (workspace-7m8.21): the tracked exports carry the work between clones,
`cairn sync` brings a store up to them, and a stale store can neither write nor be exported over
the files. Two real stores in temp directories; "git" is a copy of the four files."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from cairn.store import db_path, open_writable
from cairn.writer import WriteConflict, Writer

from .helpers import StoreCase

FILES = ("issues.jsonl", "memories.jsonl", "allocator.json", "events.jsonl")


class SyncTest(StoreCase):
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
