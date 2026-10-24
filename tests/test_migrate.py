"""Stage 4: the cutover from bd (`cairn init --from`, the allocator seed read
from bd's Dolt store), the schema migrator, and an export bd's tracked JSONL
cannot tell apart from bd's own."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from cairn import doltseed
from cairn.queries import Snapshot, go_json
from cairn.store import (SCHEMA_VERSION, StoreError, db_path, init_store, migrate, open_db,
                         open_writable)

from .helpers import StoreCase, child, dep, issue, memory, with_deps, write_jsonl


def rows():
    return [
        issue("t-aaa", priority=1),
        child("t-aaa.1", "t-aaa"),
        child("t-aaa.2", "t-aaa", status="closed", closed_at="2026-09-11T00:00:00Z"),
        with_deps(issue("t-bbb"), dep("t-bbb", "t-aaa.1")),
        memory("mem-one", "a body linking [[mem-two]]"),
        memory("mem-two", "x"),
    ]


class CutoverTest(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("BEADS_ACTOR", None)
        self.export = write_jsonl(self.workspace / "bd.jsonl", rows())

    def seed(self, used, counters) -> Path:
        path = self.workspace / "seed.json"
        path.write_text(json.dumps({"used_ids": used, "child_counters": counters}))
        return path

    def init(self, *extra: str):
        return self.cli("init", "-p", "t", "--from", str(self.export), *extra)

    def w(self, *argv: str) -> str:
        code, out, err = self.cli(*argv, "--actor", "alice")
        self.assertEqual(code, 0, f"{argv}: {err}")
        return out.strip()

    def test_seed_burns_deleted_ids_and_child_numbers(self):
        # bd once issued t-aaa.3..t-aaa.7 and a root t-zzz, all deleted since.
        seed = self.seed(["t-aaa", "t-aaa.1", "t-aaa.2", "t-aaa.7", "t-bbb", "t-zzz"],
                         {"t-aaa": 7})
        code, out, err = self.init("--seed", str(seed), "--json")
        self.assertEqual(code, 0, err)
        counts = json.loads(out)["counts"]
        self.assertEqual((counts["used_ids"], counts["seed_only_ids"]), (6, 2))
        self.assertEqual(self.w("create", "--silent", "--title", "n", "--parent", "t-aaa"),
                         "t-aaa.8")
        code, _, err = self.cli("create", "--id", "t-zzz", "--title", "again")
        self.assertEqual(code, 2)
        self.assertIn("exists or existed", err)

    def test_without_seed_counters_come_from_the_children_present(self):
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.w("create", "--silent", "--title", "n", "--parent", "t-aaa"),
                         "t-aaa.3")
        self.assertEqual(self.w("create", "--silent", "--title", "n", "--parent", "t-aaa.1"),
                         "t-aaa.1.1")

    def test_the_migrated_store_is_writable_and_answers(self):
        self.assertEqual(self.init()[0], 0)
        _, out, _ = self.cli("ready", "--json")
        self.assertEqual(sorted(i["id"] for i in json.loads(out)), ["t-aaa", "t-aaa.1"])
        self.w("update", "t-aaa.1", "--claim")
        self.w("close", "t-aaa.1")
        _, out, _ = self.cli("ready", "--json")
        self.assertIn("t-bbb", [i["id"] for i in json.loads(out)])
        _, out, _ = self.cli("recall", "mem-one")
        self.assertIn("a body linking", out)
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='writable'"
                                      ).fetchone()[0], "1")
        # shadow sync must not replace it
        code, _, err = self.cli("import", str(self.export))
        self.assertEqual(code, 2)
        self.assertIn("writable", err)

    def test_refusals_leave_nothing_behind(self):
        cases = {
            "unsupported row": [*rows(), {"_type": "wisp", "id": "t-w"}],
            "foreign prefix": [*rows(), issue("u-ccc")],
        }
        for name, data in cases.items():
            with self.subTest(name):
                write_jsonl(self.export, data)
                code, _, err = self.init()
                self.assertEqual(code, 2, name)
                self.assertFalse(db_path(self.store_dir).exists(), name)
                self.assertEqual([p.name for p in self.store_dir.glob("cairn.db*")], [], name)
        write_jsonl(self.export, rows())
        for name, seed in (("foreign seed id", self.seed(["u-x"], {})),
                           ("bad counter", self.seed([], {"t-aaa": -1})),
                           ("not json", self.workspace / "export.jsonl")):
            with self.subTest(name):
                if name == "not json":
                    seed.write_text("{")
                code, _, err = self.init("--seed", str(seed))
                self.assertEqual(code, 2, f"{name}: {err}")
                self.assertFalse(db_path(self.store_dir).exists(), name)

    def test_content_the_store_cannot_hold_is_refused(self):
        # A newer bd adding a field to comments or dependencies: the importer has
        # no column for it, so the migration must refuse rather than drop it.
        future = [
            {**issue("t-c"), "comments": [{"id": "c1", "issue_id": "t-c", "author": "a",
                                           "text": "x", "created_at": "2026-09-02T00:00:00Z",
                                           "edited_at": "2026-09-03T00:00:00Z"}]},
            with_deps(issue("t-d"), {**dep("t-d", "t-c"), "weight": 3}),
        ]
        for row in future:
            with self.subTest(row["id"]):
                write_jsonl(self.export, [*rows(), row])
                code, _, err = self.init()
                self.assertEqual(code, 2, err)
                self.assertIn("would not hold what the source holds", err)
                self.assertFalse(db_path(self.store_dir).exists())

    def test_reports_whether_the_layout_matched(self):
        code, out, err = self.init("--json")
        self.assertEqual(code, 0, err)
        # helpers write keys in their own order, not bd's: equal content, other layout
        self.assertIs(json.loads(out)["byte_identical"], False)
        export = self.workspace / "again.jsonl"
        self.cli("export", "--all", "-o", str(export))
        self.store_dir.rename(self.workspace / "first")
        code, out, err = self.cli("init", "-p", "t", "--from", str(export), "--json")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["byte_identical"], True)
        self.assertEqual(json.loads(out)["bd_version"], None)

    def test_a_derived_count_that_differs_is_a_warning(self):
        write_jsonl(self.export, [*rows()[:-2], {**issue("t-z"), "dependency_count": 7},
                                  *rows()[-2:]])
        code, out, err = self.init("--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(any("derived count" in w for w in json.loads(out)["warnings"]))

    def test_prime_says_who_writes(self):
        self.assertEqual(self.init()[0], 0)
        _, out, _ = self.cli("prime")
        self.assertIn("cairn is its only writer", out)
        self.assertNotIn("Shadow mode", out)

    def test_init_honours_cairn_dir(self):
        target = self.workspace / "elsewhere"
        os.environ["CAIRN_DIR"] = str(target)
        self.addCleanup(os.environ.pop, "CAIRN_DIR", None)
        self.assertEqual(self.init()[0], 0)
        self.assertTrue(db_path(target).is_file())
        self.assertFalse(db_path(self.store_dir).exists())

    def test_existing_store_and_stray_options_are_refused(self):
        code, _, err = self.cli("init", "-p", "t", "--seed", str(self.export))
        self.assertEqual(code, 2)
        self.assertIn("need --from", err)
        self.assertEqual(self.init()[0], 0)
        code, _, err = self.init()
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)


class FreshCheckoutTest(StoreCase):
    """What git carries (the two JSONL exports and the allocator state) rebuilds a store that
    reissues nothing: the 'fresh checkout' clause of stage 4."""

    def test_rebuild_from_tracked_files_burns_deleted_ids(self):
        os.environ.pop("BEADS_ACTOR", None)
        live = self.workspace / "live"
        live.mkdir()
        self.assertEqual(self.cli("init", "-p", "t", cwd=live)[0], 0)
        w = lambda *a: self.cli(*a, "--actor", "al", cwd=live)[1].strip()  # noqa: E731
        root = w("create", "--silent", "--title", "root")
        kids = [w("create", "--silent", "--title", f"k{i}", "--parent", root) for i in range(3)]
        gone = w("create", "--silent", "--title", "gone")
        self.cli("delete", kids[2], "--force", "--actor", "al", cwd=live)
        self.cli("delete", gone, "--force", "--actor", "al", cwd=live)
        self.cli("remember", "--key", "m", "body", "--actor", "al", cwd=live)
        issues, mems, alloc = (self.workspace / n for n in ("issues.jsonl", "memories.jsonl",
                                                            "allocator.json"))
        _, out, _ = self.cli("export", "--all", cwd=live)
        issues.write_text("".join(l + "\n" for l in out.splitlines() if '"_type":"issue"' in l))
        mems.write_text("".join(l + "\n" for l in out.splitlines() if '"_type":"memory"' in l))
        self.assertEqual(self.cli("export", "--allocator", "-o", str(alloc), cwd=live)[0], 0)
        seed = json.loads(alloc.read_text())
        self.assertIn(gone, seed["used_ids"])
        self.assertEqual(seed["child_counters"][root], 3)

        fresh = self.workspace / "fresh"
        fresh.mkdir()
        both = self.workspace / "both.jsonl"
        both.write_text(issues.read_text() + mems.read_text())
        code, _, err = self.cli("init", "-p", "t", "--from", str(both), "--seed", str(alloc),
                                cwd=fresh)
        self.assertEqual(code, 0, err)
        f = lambda *a: self.cli(*a, "--actor", "al", cwd=fresh)  # noqa: E731
        self.assertEqual(f("create", "--silent", "--title", "n", "--parent", root)[1].strip(),
                         f"{root}.4")
        code, _, err = f("create", "--id", gone, "--title", "again")
        self.assertEqual(code, 2)
        self.assertIn("existed", err)
        self.assertIn("body", self.cli("recall", "m", cwd=fresh)[1])

    def test_allocator_export_refuses_a_shadow(self):
        self.build([issue("t-a")])
        code, _, err = self.cli("export", "--allocator")
        self.assertEqual(code, 2)
        self.assertIn("shadow", err)


class EventsExportTest(StoreCase):
    """workspace-7m8.15: the history leaves the box. `export --events` is a deterministic file
    git can carry beside the JSONL exports, and `init --from ... --events` brings it back."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("BEADS_ACTOR", None)

    def live(self, name: str = "live") -> Path:
        d = self.workspace / name
        d.mkdir()
        self.assertEqual(self.cli("init", "-p", "t", cwd=d)[0], 0)
        return d

    def w(self, cwd: Path, *argv: str) -> str:
        code, out, err = self.cli(*argv, "--actor", "al", cwd=cwd)
        self.assertEqual(code, 0, f"{argv}: {err}")
        return out.strip()

    def test_shape_order_and_rebuild(self):
        live = self.live()
        root = self.w(live, "create", "--silent", "--title", "root")
        kid = self.w(live, "create", "--silent", "--title", "kid", "--parent", root)
        self.w(live, "update", kid, "--claim")
        self.w(live, "close", kid)
        self.w(live, "remember", "--key", "m", "a body")
        self.w(live, "delete", kid, "--force")
        code, out, err = self.cli("export", "--events", cwd=live)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        rows = [json.loads(l) for l in lines]
        conn = sqlite3.connect(db_path(live / ".cairn"))
        self.addCleanup(conn.close)
        self.assertEqual(len(rows), conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertGreaterEqual(len(rows), 6)
        for row in rows:
            self.assertEqual(list(row), ["_type", "id", "kind", "issue_id", "memory_key",
                                         "actor", "created_at", "data"])
            self.assertEqual(row["_type"], "event")
            self.assertIsInstance(row["data"], dict)
            self.assertEqual(row["actor"], "al")
        stamps = [r["created_at"] for r in rows]
        self.assertEqual(stamps, sorted(stamps))
        self.assertIn("m", {r["memory_key"] for r in rows})
        self.assertEqual(self.cli("export", "--events", cwd=live)[1], out, "two exports differ")

        # What git carries rebuilds the history, byte for byte.
        issues, mems, alloc, events = (self.workspace / n for n in (
            "issues.jsonl", "memories.jsonl", "allocator.json", "events.jsonl"))
        _, all_rows, _ = self.cli("export", "--all", cwd=live)
        (self.workspace / "both.jsonl").write_text(all_rows)
        self.assertEqual(self.cli("export", "--allocator", "-o", str(alloc), cwd=live)[0], 0)
        code, msg, err = self.cli("export", "--events", "-o", str(events), cwd=live)
        self.assertEqual(code, 0, err)
        self.assertEqual(msg, "", "the notice goes to stderr, as bd's does")
        self.assertIn(f"Exported {len(rows)} events", err)
        self.assertEqual(events.read_text(), out)
        fresh = self.workspace / "fresh"
        fresh.mkdir()
        code, report, err = self.cli("init", "-p", "t", "--from", str(self.workspace / "both.jsonl"),
                                     "--seed", str(alloc), "--events", str(events), "--json",
                                     cwd=fresh)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(report)["counts"]["events"], len(rows))
        self.assertEqual(self.cli("export", "--events", cwd=fresh)[1], out)
        # A later write appends: the old file is a prefix of the new one (git sees an append).
        self.w(fresh, "create", "--silent", "--title", "later")
        later = self.cli("export", "--events", cwd=fresh)[1]
        self.assertTrue(later.startswith(out))
        self.assertEqual(len(later.splitlines()), len(rows) + 1)

    def test_events_is_a_file_of_its_own(self):
        live = self.live()
        for extra in (("--all",), ("--include-memories",), ("--allocator",)):
            code, _, err = self.cli("export", "--events", *extra, cwd=live)
            self.assertEqual(code, 2, extra)
            self.assertIn("alone", err)
        _, out, _ = self.cli("export", "--all", cwd=live)
        self.assertNotIn('"_type":"event"', out)

    def test_init_refuses_a_bad_events_file_and_writes_nothing(self):
        both = write_jsonl(self.workspace / "both.jsonl", [issue("t-a")])
        good = {"_type": "event", "id": "ev-1", "kind": "created", "issue_id": "t-a",
                "memory_key": None, "actor": "al", "created_at": "2026-10-01T00:00:00Z",
                "data": {"title": "x"}}
        cases = {
            "not an event row": [dict(good, _type="issue")],
            "missing or empty 'kind'": [dict(good, kind="")],
            "duplicate event id": [good, dict(good, created_at="2026-10-02T00:00:00Z")],
            "'data' must be an object": [dict(good, data="x")],
            "unknown key(s) from_the_future": [dict(good, from_the_future=1)],
        }
        for message, rows_ in cases.items():
            with self.subTest(message):
                target = self.workspace / message.replace(" ", "_").replace("'", "")
                target.mkdir()
                bad = write_jsonl(self.workspace / "bad.jsonl", rows_)
                code, _, err = self.cli("init", "-p", "t", "--from", str(both), "--events",
                                        str(bad), cwd=target)
                self.assertEqual(code, 2, err)
                self.assertIn(message, err)
                self.assertFalse(db_path(target / ".cairn").exists())
        # bd's audit log and an events file naming the same id: refused, not merged.
        log = write_jsonl(self.workspace / "interactions.jsonl", [{
            "id": "ev-1", "kind": "field_change", "created_at": "2026-09-01T10:00:00Z",
            "actor": "ann", "issue_id": "t-a", "extra": {}}])
        ev = write_jsonl(self.workspace / "ev.jsonl", [good])
        target = self.workspace / "clash"
        target.mkdir()
        code, _, err = self.cli("init", "-p", "t", "--from", str(both), "--interactions",
                                str(log), "--events", str(ev), cwd=target)
        self.assertEqual(code, 2, err)
        self.assertIn("UNIQUE", err)
        self.assertFalse(db_path(target / ".cairn").exists())
        # Needs --from, like the other sources.
        code, _, err = self.cli("init", "-p", "t", "--events", str(ev), cwd=self.workspace)
        self.assertEqual(code, 2)
        self.assertIn("--events", err)

    def test_init_refuses_when_the_history_would_not_come_back(self):
        # The self-check's failure branch. Nothing in the importer can make it fire on its own
        # (what is inserted is what is read back), so the read-back is faked to drop a field.
        from unittest import mock
        from cairn import queries
        both = write_jsonl(self.workspace / "both.jsonl", [issue("t-a")])
        ev = write_jsonl(self.workspace / "ev.jsonl", [{
            "_type": "event", "id": "ev-1", "kind": "created", "issue_id": "t-a",
            "memory_key": None, "actor": "al", "created_at": "2026-10-01T00:00:00Z", "data": {}}])
        real = queries.event_objects

        def lossy(conn):
            rows = real(conn)
            for row in rows:
                row["actor"] = None
            return rows
        target = self.workspace / "lossy"
        target.mkdir()
        with mock.patch.object(queries, "event_objects", lossy):
            code, _, err = self.cli("init", "-p", "t", "--from", str(both), "--events", str(ev),
                                    cwd=target)
        self.assertEqual(code, 2, err)
        self.assertIn("would not come back as they went in", err)
        self.assertIn("ev-1", err)
        self.assertFalse(db_path(target / ".cairn").exists())

    def test_imported_bd_history_is_exported_too(self):
        log = write_jsonl(self.workspace / "interactions.jsonl", [{
            "id": "int-1", "kind": "field_change", "created_at": "2026-09-01T10:00:00Z",
            "actor": "ann", "issue_id": "t-a", "extra": {"field": "status", "new": "closed"}}])
        self.build([issue("t-a")], interactions_path=log)
        code, out, err = self.cli("export", "--events")
        self.assertEqual(code, 0, err)
        rows = [json.loads(l) for l in out.splitlines()]
        self.assertEqual([r["id"] for r in rows], ["int-1"])
        self.assertEqual(rows[0]["data"], {"field": "status", "new": "closed"})
        self.assertIsNone(rows[0]["memory_key"])


class WorktreeRedirectTest(StoreCase):
    def test_a_redirect_names_the_main_store(self):
        os.environ.pop("BEADS_ACTOR", None)
        self.assertEqual(self.cli("init", "-p", "t")[0], 0)
        tree = self.workspace / "wt"
        (tree / ".cairn").mkdir(parents=True)
        (tree / ".cairn" / "config.json").write_text("{}")
        (tree / ".cairn" / "redirect").write_text(str(self.store_dir.resolve()) + "\n")
        _, out, _ = self.cli("create", "--silent", "--title", "from the worktree",
                             "--actor", "al", cwd=tree)
        _, listing, _ = self.cli("list", "--json")
        self.assertIn(out.strip(), [i["id"] for i in json.loads(listing)])
        self.assertFalse((tree / ".cairn" / "cairn.db").exists())

    def test_bad_redirects_are_errors(self):
        tree = self.workspace / "wt"
        (tree / ".cairn").mkdir(parents=True)
        for text, needle in (("relative/path", "absolute"),
                             (str(self.workspace / "nowhere"), "not a directory")):
            (tree / ".cairn" / "redirect").write_text(text)
            code, _, err = self.cli("list", cwd=tree)
            self.assertEqual(code, 2)
            self.assertIn(needle, err)


class ExportFormatTest(StoreCase):
    """bd's tracked JSONL, reproduced byte for byte (measured on the workspace
    store, 244 issues, bd 1.1.2): rows by priority, newest first, then id; Go's
    escaping. Key order and dependency order are read from the source, because
    bd changed the dependency order between 1.1.2 and 1.3.1."""

    def test_row_order_and_escaping(self):
        self.build([
            issue("t-old", created_at="2026-09-01T00:00:00Z", description="a < b && c > d"),
            issue("t-new", created_at="2026-09-02T00:00:00Z"),
            issue("t-tie2", created_at="2026-09-02T00:00:00Z"),
            issue("t-tie1", created_at="2026-09-02T00:00:00Z"),
            issue("t-p0", priority=0, created_at="2026-08-01T00:00:00Z"),
        ])
        _, out, _ = self.cli("export")
        lines = out.splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines],
                         ["t-p0", "t-new", "t-tie1", "t-tie2", "t-old"])
        self.assertIn("a \\u003c b \\u0026\\u0026 c \\u003e d", lines[-1])

    def test_key_order_follows_the_source_including_unknown_fields(self):
        row = {"_type": "issue", "id": "t-a", "title": "x", "from_the_future": "kept",
               "description": "d", "status": "open", "priority": 2, "issue_type": "task",
               "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"}
        self.build([row])
        _, out, _ = self.cli("export")
        self.assertEqual(list(json.loads(out))[:5],
                         ["_type", "id", "title", "from_the_future", "description"])

    def test_without_a_source_keys_follow_bd(self):
        os.environ.pop("BEADS_ACTOR", None)
        self.assertEqual(self.cli("init", "-p", "t")[0], 0)
        _, issue_id, _ = self.cli("create", "--silent", "--title", "x", "-l", "a",
                                  "--actor", "al")
        self.cli("comment", issue_id.strip(), "hi", "--actor", "al")
        _, out, _ = self.cli("export")
        keys = list(json.loads(out))
        self.assertEqual(keys[keys.index("labels"):keys.index("labels") + 2],
                         ["labels", "comments"])

    def test_dependency_order_is_read_from_the_source(self):
        # bd 1.1.2 lists by edge id (sqd's sorts first, measured); bd 1.3.1 by target.
        for order, profile in ((["workspace-sqd", "workspace-6oa"], "edge-id"),
                               (["workspace-6oa", "workspace-sqd"], "target")):
            with self.subTest(profile):
                self.setUp()
                self.build([
                    with_deps(issue("workspace-3rj"),
                              *(dep("workspace-3rj", t) for t in order)),
                    issue("workspace-6oa"), issue("workspace-sqd"),
                ])
                _, out, _ = self.cli("export")
                row = next(json.loads(l) for l in out.splitlines()
                           if json.loads(l)["id"] == "workspace-3rj")
                self.assertEqual([d["depends_on_id"] for d in row["dependencies"]], order)

    def test_go_json(self):
        self.assertEqual(go_json({"a": "<>&\u2028\u2029é"}),
                         '{"a":"\\u003c\\u003e\\u0026\\u2028\\u2029é"}')


class MigrateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "cairn.db"
        init_store(self.db, "t").close()

    def version(self) -> str:
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                                ).fetchone()[0]
        finally:
            conn.close()

    def test_current_store_needs_nothing(self):
        self.assertEqual(migrate(self.db), (SCHEMA_VERSION, SCHEMA_VERSION, None))

    def test_steps_apply_in_one_transaction_after_a_backup(self):
        steps = {1: ["ALTER TABLE issues ADD COLUMN x TEXT"],
                 2: ["CREATE TABLE extra(k TEXT PRIMARY KEY)"]}
        start, end, backup = migrate(self.db, migrations=steps, target=3)
        self.assertEqual((start, end, self.version()), (1, 3, "3"))
        old = sqlite3.connect(str(backup))
        try:
            self.assertEqual(old.execute("SELECT value FROM meta WHERE key='schema_version'"
                                         ).fetchone()[0], "1")
        finally:
            old.close()

    def test_a_failing_step_changes_nothing_and_removes_its_backup(self):
        steps = {1: ["ALTER TABLE issues ADD COLUMN x TEXT"], 2: ["THIS IS NOT SQL"]}
        with self.assertRaises(sqlite3.Error):
            migrate(self.db, migrations=steps, target=3)
        self.assertEqual(self.version(), "1")
        self.assertEqual(list(self.db.parent.glob("*.bak")), [])
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(issues)")]
        self.assertNotIn("x", cols)

    def test_missing_step_newer_store_and_shadow_are_refused(self):
        with self.assertRaisesRegex(StoreError, "no migration from schema version 1"):
            migrate(self.db, migrations={}, target=2)
        with self.assertRaisesRegex(StoreError, "upgrade cairn"):
            migrate(self.db, migrations={}, target=0)
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE meta SET value='0' WHERE key='writable'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(StoreError, "shadow sync"):
            migrate(self.db, migrations={1: []}, target=2)

    def test_an_older_store_points_at_migrate(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        for opener in (open_db, open_writable):
            with self.assertRaisesRegex(StoreError, "cairn migrate"):
                opener(self.db)


FAKE_DOLT = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["config"]:
    sys.exit(0)
assert os.path.isdir(".dolt"), "not run inside a dolt repo"
open(os.environ["FAKE_DOLT_LOG"], "a").write(os.getcwd() + "\n")
q = args[-1]
data = json.loads(os.environ["FAKE_DOLT_DATA"])
if q == "SHOW TABLES":
    rows = [{"Tables_in_x": t} for t in data.get("tables", ["issues", "child_counters",
                                                              "local_metadata"])]
elif "local_metadata" in q:
    rows = [{"value": data["version"]}] if data.get("version") else []
elif "dolt_history_issues" in q:
    rows = [{"id": i} for i in data["history"]]
elif "child_counters" in q:
    rows = [{"parent_id": p, "last_child": n} for p, n in data["counters"].items()]
else:
    rows = [{"id": i} for i in data["current"]]
print(json.dumps({"rows": rows}))
'''


class DoltSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.beads = base / ".beads"
        (self.beads / "embeddeddolt" / "workspace" / ".dolt").mkdir(parents=True)
        self.dolt = base / "dolt"
        self.dolt.write_text(FAKE_DOLT.replace("/usr/bin/env python3", sys.executable))
        self.dolt.chmod(self.dolt.stat().st_mode | stat.S_IEXEC)
        self.log = base / "log"
        os.environ["FAKE_DOLT_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "FAKE_DOLT_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_DOLT_DATA", None)

    def data(self, **d) -> None:
        os.environ["FAKE_DOLT_DATA"] = json.dumps(d)

    def test_reads_history_and_counters_from_a_copy(self):
        self.data(history=["t-a", "t-a.1", "t-a.2", "t-z"], current=["t-a", "t-a.1"],
                  counters={"t-a": 2})
        seed = doltseed.read_seed(self.beads, str(self.dolt))
        self.assertEqual(seed["used_ids"], ["t-a", "t-a.1", "t-a.2", "t-z"])
        self.assertEqual(seed["deleted_ids"], ["t-a.2", "t-z"])
        self.assertEqual(seed["child_counters"], {"t-a": 2})
        ran_in = set(self.log.read_text().split())
        self.assertNotIn(str(self.beads / "embeddeddolt" / "workspace"), ran_in)

    def test_a_counter_above_every_historical_child_is_kept(self):
        # bd issued t-a.1..t-a.9, then deleted 5..9 before the history began: only the
        # counter knows. The seed must carry 9, not the 4 the history alone would give.
        self.data(history=["t-a", "t-a.1", "t-a.4"], current=["t-a", "t-a.1", "t-a.4"],
                  counters={"t-a": 9})
        self.assertEqual(doltseed.read_seed(self.beads, str(self.dolt))["child_counters"],
                         {"t-a": 9})

    def test_version_and_a_store_without_child_counters(self):
        self.data(history=["t-a", "t-a.1", "t-a.4", "t-a.4.2"], current=["t-a"],
                  counters={}, version="9.0.0", tables=["issues", "local_metadata"])
        seed = doltseed.read_seed(self.beads, str(self.dolt))
        self.assertEqual(seed["bd_version"], "9.0.0")
        self.assertEqual(seed["child_counters"], {"t-a": 4, "t-a.4": 2})
        self.assertIn("derived from the id history", seed["notes"][0])

    def test_server_mode_layout_is_found(self):
        server = self.beads.parent / "srv" / ".beads" / "dolt" / "proj" / ".dolt"
        server.mkdir(parents=True)
        self.assertEqual(doltseed.find_dolt_repo(self.beads.parent / "srv"),
                         server.parent.resolve())

    def test_history_missing_a_current_id_is_an_error(self):
        self.data(history=["t-a"], current=["t-a", "t-b"], counters={})
        with self.assertRaisesRegex(doltseed.DoltError, "lacks current ids"):
            doltseed.read_seed(self.beads, str(self.dolt))

    def test_no_dolt_and_no_repo(self):
        with self.assertRaisesRegex(doltseed.DoltError, "not found"):
            doltseed.read_seed(self.beads, str(self.dolt) + "-missing")
        with self.assertRaisesRegex(doltseed.DoltError, "no Dolt repository"):
            doltseed.find_dolt_repo(self.beads.parent / "nowhere")


if __name__ == "__main__":
    unittest.main()
