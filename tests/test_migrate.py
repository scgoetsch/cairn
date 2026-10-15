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


class ExportFormatTest(StoreCase):
    """bd's tracked JSONL, reproduced byte for byte (measured on the workspace
    store, 244 issues, bd 1.1.2): rows by priority, newest first, then id; keys
    in bd's struct order; dependencies by bd's UUIDv5 edge id; Go's escaping."""

    def test_row_order_key_order_and_escaping(self):
        self.build([
            issue("t-old", created_at="2026-09-01T00:00:00Z", description="a < b && c > d"),
            issue("t-new", created_at="2026-09-02T00:00:00Z", labels=["x"],
                  external_ref="ext:1",
                  comments=[{"id": "c1", "issue_id": "t-new", "author": "a", "text": "hi",
                             "created_at": "2026-09-02T00:00:00Z"}]),
            issue("t-tie2", created_at="2026-09-02T00:00:00Z"),
            issue("t-tie1", created_at="2026-09-02T00:00:00Z"),
            issue("t-p0", priority=0, created_at="2026-08-01T00:00:00Z"),
        ])
        _, out, _ = self.cli("export")
        lines = out.splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines],
                         ["t-p0", "t-new", "t-tie1", "t-tie2", "t-old"])
        new = json.loads(lines[1])
        keys = list(new)
        self.assertLess(keys.index("close_reason") if "close_reason" in keys else 0,
                        keys.index("external_ref"))
        self.assertEqual(keys[keys.index("labels"):keys.index("labels") + 2],
                         ["labels", "comments"])
        self.assertIn("a \\u003c b \\u0026\\u0026 c \\u003e d", lines[-1])

    def test_dependencies_in_bd_edge_id_order(self):
        # bd's ids for these two edges: ...-sqd sorts before ...-6oa (measured).
        self.build([
            with_deps(issue("workspace-3rj"), dep("workspace-3rj", "workspace-6oa"),
                      dep("workspace-3rj", "workspace-sqd")),
            issue("workspace-6oa"), issue("workspace-sqd"),
        ])
        _, out, _ = self.cli("export")
        row = next(json.loads(l) for l in out.splitlines() if "3rj" in l.split(",")[1])
        self.assertEqual([d["depends_on_id"] for d in row["dependencies"]],
                         ["workspace-sqd", "workspace-6oa"])

    def test_go_json(self):
        self.assertEqual(go_json({"a": "<>&  é"}),
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
if "dolt_history_issues" in q:
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
