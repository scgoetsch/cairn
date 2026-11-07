"""`cairn history-from-dolt` (workspace-7m8.5.1): bd's Dolt history as cairn events.

The reader tests build a small Dolt store with bd 1.1.2's table names and run the real `dolt`
CLI against it (skipped when no dolt is installed: bd ships none). The merge tests need nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from cairn import dolthistory
from cairn.eventdata import apply_field_change
from cairn.importer import body_hash

from .helpers import StoreCase

DOLT = os.environ.get("CAIRN_TEST_DOLT") or shutil.which("dolt")

SCHEMA = """
CREATE TABLE issues (id varchar(255) PRIMARY KEY, title varchar(500), description text,
  design text, acceptance_criteria text, notes text, status varchar(32), priority int,
  issue_type varchar(32), assignee varchar(255), owner varchar(255), created_at datetime,
  created_by varchar(255), updated_at datetime, started_at datetime, closed_at datetime,
  close_reason text, defer_until datetime, content_hash varchar(64));
CREATE TABLE config (`key` varchar(255) PRIMARY KEY, `value` text);
CREATE TABLE labels (issue_id varchar(255), label varchar(255), PRIMARY KEY (issue_id, label));
CREATE TABLE dependencies (id varchar(64) PRIMARY KEY, issue_id varchar(255), type varchar(32),
  depends_on_issue_id varchar(255));
CREATE TABLE comments (id varchar(64) PRIMARY KEY, issue_id varchar(255), author varchar(255),
  text text, created_at datetime);
CREATE TABLE events (id varchar(64) PRIMARY KEY, issue_id varchar(255), event_type varchar(32),
  actor varchar(255), old_value longtext, new_value longtext, comment text, created_at datetime);
"""


@unittest.skipUnless(DOLT, "no dolt CLI (set CAIRN_TEST_DOLT)")
class ReaderTest(unittest.TestCase):
    """One bd-shaped history: create, edit, note twice, a memory rewritten twice and one
    forgotten, a label, a parent, a comment; every step one commit with one actor."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cairn-dolthist-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {**os.environ, "HOME": str(self.home), "DOLT_ROOT_PATH": str(self.home)}
        for key, value in (("user.name", "beads"), ("user.email", "b@localhost")):
            subprocess.run([DOLT, "config", "--global", "--add", key, value], env=self.env,
                           check=True, capture_output=True)
        self.repo = self.tmp / ".beads" / "embeddeddolt" / "workspace"
        self.repo.mkdir(parents=True)
        self.dolt("init")
        self.n = 0
        self.commit(SCHEMA, actor=None)

    def dolt(self, *argv: str) -> str:
        proc = subprocess.run([DOLT, *argv], cwd=self.repo, env=self.env, capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def commit(self, sql: str, *, actor: Any = "alice", kind: str = "updated",
               issue: str = "t-1") -> None:
        self.n += 1
        if actor:
            sql += (f"; INSERT INTO events VALUES ('e{self.n}', '{issue}', '{kind}', '{actor}', "  # noqa: S608 — test-literal values in a throwaway Dolt repo
                    f"NULL, NULL, NULL, '2026-05-0{min(self.n, 9)} 10:00:00')")
        self.dolt("sql", "-q", sql)
        self.dolt("add", "-A")
        self.dolt("commit", "-m", f"step {self.n}", "--date", f"2026-05-01T10:{self.n:02d}:00")

    def read(self, **kw: Any) -> Any:
        with dolthistory.dolt_copy(self.tmp / ".beads", DOLT) as (_, query):
            return dolthistory.read_history(query, **kw)

    def build(self) -> None:
        self.commit("INSERT INTO issues (id, title, status, priority, issue_type, created_at) "
                    "VALUES ('t-1', 'first title', 'open', 2, 'task', '2026-05-01 10:01:00');"
                    "INSERT INTO config VALUES ('kv.memory.m', 'one <&>')", kind="created")
        self.commit("UPDATE issues SET title = 'second title', description = 'd' "
                    "WHERE id = 't-1'")
        self.commit("UPDATE issues SET notes = 'n1' WHERE id = 't-1'")
        self.commit("UPDATE issues SET notes = 'n1\nn2 café' WHERE id = 't-1'")
        self.commit("UPDATE config SET `value` = 'two' WHERE `key` = 'kv.memory.m';"
                    "INSERT INTO config VALUES ('kv.memory.gone', 'bye'), ('other', 'x')",
                    actor="bob")
        self.commit("UPDATE config SET `value` = 'three' WHERE `key` = 'kv.memory.m';"
                    "DELETE FROM config WHERE `key` = 'kv.memory.gone'")
        self.commit("INSERT INTO issues (id, title, status, priority, issue_type) VALUES "
                    "('t-1.1', 'child', 'open', 2, 'task');"
                    "INSERT INTO dependencies VALUES ('d1', 't-1.1', 'parent-child', 't-1');"
                    "INSERT INTO labels VALUES ('t-1', 'x');"
                    "INSERT INTO comments VALUES ('c1', 't-1', 'carol', 'hi', "
                    "'2026-05-01 10:07:00')", kind="created", issue="t-1.1")
        self.commit("UPDATE issues SET status = 'closed', close_reason = 'done', "
                    "closed_at = '2026-05-01 10:08:00' WHERE id = 't-1'", kind="closed")

    EXPORT = [
        {"_type": "issue", "id": "t-1", "title": "second title", "description": "d",
         "notes": "n1\nn2 caf\u00e9", "status": "closed", "close_reason": "done",
         "closed_at": "2026-05-01T10:08:00Z", "priority": 2, "issue_type": "task",
         "created_at": "2026-05-01T10:01:00Z"},
        {"_type": "issue", "id": "t-1.1", "title": "child", "status": "open", "priority": 2,
         "issue_type": "task"},
        {"_type": "memory", "key": "m", "value": "three"},
    ]

    def test_the_history_comes_back_as_cairn_events(self):
        self.build()
        events, manifest = self.read(export=self.EXPORT)
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for e in events:
            by_kind.setdefault(e["kind"], []).append(e)
            self.assertTrue(e["id"].startswith("dolt-"))
            self.assertIn("dolt_commit", e["data"])
            self.assertRegex(e["created_at"], r"^2026-05-01T10:\d\d:00\.000Z$")
        self.assertEqual([e["issue_id"] for e in by_kind["created"]], ["t-1", "t-1.1"])
        self.assertEqual(by_kind["created"][1]["data"]["parent"], "t-1")
        changes = [(e["data"]["field"], {k: v for k, v in e["data"].items()
                                         if k not in ("field", "dolt_commit")})
                   for e in by_kind["field_change"]]
        self.assertEqual(changes, [
            ("title", {"old_value": "first title", "new_value": "second title"}),
            ("description", {"old_value": None, "new_value": "d"}),
            ("notes", {"old_value": None, "new_value": "n1"}),
            ("notes", {"appended": "\nn2 café"}),
            ("status", {"old_value": "open", "new_value": "closed"}),
            ("closed_at", {"old_value": None, "new_value": "2026-05-01T10:08:00Z"}),
            ("close_reason", {"old_value": None, "new_value": "done"}),
        ])
        text = None
        for e in by_kind["field_change"]:
            if e["data"]["field"] == "notes":
                text = apply_field_change(text, e["data"])
        self.assertEqual(text, "n1\nn2 café")
        mem = [(e["kind"], e["memory_key"], e["data"].get("old_value"), e["data"]["old_hash"])
               for e in events if e["memory_key"]]
        # commit order; inside one commit (step 5 and 6) the order of Dolt's diff rows
        self.assertEqual(sorted(mem[1:3]), [("memory_set", "gone", None, None),
                                            ("memory_set", "m", "one <&>", body_hash("one <&>"))])
        self.assertEqual(sorted(mem[3:5]), [("memory_deleted", "gone", "bye", body_hash("bye")),
                                            ("memory_set", "m", "two", body_hash("two"))])
        self.assertEqual((mem[0], len(mem)), (("memory_set", "m", None, None), 5))
        self.assertEqual([e["data"]["label"] for e in by_kind["label_added"]], ["x"])
        self.assertEqual(by_kind["dependency_added"][0]["data"],
                         {"depends_on_id": "t-1", "type": "parent-child",
                          "dolt_commit": by_kind["dependency_added"][0]["data"]["dolt_commit"]})
        self.assertEqual(by_kind["comment_added"][0]["actor"], "carol")
        # actors come from bd's events table, one per commit
        self.assertEqual({e["actor"] for e in events if e["memory_key"] == "gone"}, {"bob", "alice"})
        self.assertEqual(manifest["config_changes_not_memories"], 1)
        self.assertIn("content_hash", manifest["issue_columns_not_carried"])
        self.assertEqual(manifest["carried"]["memory_deleted"], 1)

    def test_two_runs_give_the_same_events(self):
        self.build()
        first, _ = self.read()
        second, _ = self.read()
        self.assertEqual(first, second)

    def test_memories_kept_elsewhere_fail_the_read(self):
        self.commit("INSERT INTO issues (id, title, status, priority, issue_type) VALUES "
                    "('t-1', 'x', 'open', 2, 'task')")
        with self.assertRaises(dolthistory.HistoryError):
            self.read(export=[{"_type": "memory", "key": "m", "value": "v"}])

    def test_a_history_that_does_not_end_at_the_export_fails_the_read(self):
        """Grok's review of d0eecba: a stale copy or a gapped log must not write a plausible
        file. Each difference alone refuses."""
        self.build()
        self.read(export=self.EXPORT)                                   # ends there: fine
        for row, field, value in ((2, "value", "two"), (0, "notes", "n1"),
                                  (1, "status", "closed")):
            export = [dict(r) for r in self.EXPORT]
            export[row][field] = value
            with self.assertRaises(dolthistory.HistoryError, msg=f"{field}={value}"):
                self.read(export=export)
        with self.assertRaises(dolthistory.HistoryError):
            self.read(export=[*self.EXPORT, {"_type": "memory", "key": "new", "value": "x"}])
        for gone in range(3):                       # Dolt has it, the export does not
            with self.assertRaises(dolthistory.HistoryError, msg=str(gone)):
                self.read(export=[r for i, r in enumerate(self.EXPORT) if i != gone])
        export = [dict(r) for r in self.EXPORT]
        del export[0]["issue_type"]                 # an omitted field is an empty one
        with self.assertRaises(dolthistory.HistoryError):
            self.read(export=export)

    def test_an_embedded_and_a_server_store_side_by_side_are_refused(self):
        self.build()
        shutil.copytree(self.repo, self.tmp / ".beads" / "dolt" / "workspace")
        with self.assertRaises(dolthistory.HistoryError):
            self.read(export=self.EXPORT)

    def test_every_bd_events_row_is_accounted_for(self):
        """Acceptance (3): each row of bd's events table, recounted from Dolt itself."""
        self.build()
        self.commit("UPDATE labels SET label = label WHERE issue_id = 't-1'")   # no change
        _, manifest = self.read(export=self.EXPORT)
        rows = json.loads(self.dolt("sql", "-r", "json", "-q",
                                    "SELECT COUNT(*) AS n FROM dolt_diff_events "
                                    "WHERE to_commit <> 'WORKING'"))["rows"][0]["n"]
        counted = sum(n for kinds in manifest["bd_events"].values() for n in kinds.values())
        self.assertEqual(counted, rows)
        self.assertEqual(manifest["bd_events"]["updated"]
                         .get("not imported; its commit changed nothing this reader carries"), 1)

    def test_a_re_created_issue_starts_a_new_notes_chain(self):
        self.commit("INSERT INTO issues (id, title, status, priority, issue_type) VALUES "
                    "('t-1', 'x', 'open', 2, 'task')", kind="created")
        self.commit("UPDATE issues SET notes = 'a' WHERE id = 't-1'")
        self.commit("UPDATE issues SET notes = 'a b' WHERE id = 't-1'")
        self.commit("DELETE FROM issues WHERE id = 't-1'", kind="deleted")
        self.commit("INSERT INTO issues (id, title, status, priority, issue_type, notes) VALUES "
                    "('t-1', 'x', 'open', 2, 'task', 'hello')", kind="created")
        self.commit("UPDATE issues SET notes = 'hello world' WHERE id = 't-1'")
        events, _ = self.read()
        notes = [e["data"] for e in events
                 if e["kind"] == "field_change" and e["data"]["field"] == "notes"]
        self.assertEqual([sorted(k for k in n if k != "dolt_commit") for n in notes],
                         [["field", "new_value", "old_value"], ["appended", "field"],
                          ["field", "new_value", "old_value"]])
        self.assertEqual(notes[-1]["old_value"], "hello")

    def test_a_store_without_bd_tables_is_refused(self):
        self.dolt("sql", "-q", "DROP TABLE config")
        self.dolt("add", "-A")
        self.dolt("commit", "-m", "drop")
        with self.assertRaises(dolthistory.HistoryError):
            self.read()


def ev(eid: str, when: str, issue: str, **data: Any) -> Dict[str, Any]:
    return {"_type": "event", "id": eid, "kind": "field_change", "issue_id": issue,
            "memory_key": None, "actor": "a", "created_at": when, "data": data}


class MergeTest(unittest.TestCase):
    def test_a_change_both_sources_hold_is_kept_once_and_the_rest_merges_by_time(self):
        earlier = [ev("int-1", "2026-05-01T10:00:00.019989276Z", "t-1", field="status",
                      old_value="open", new_value="closed"),
                   ev("int-2", "2026-05-01T12:00:00Z", "t-1", field="priority",
                      old_value="2", new_value="1")]
        dolt = [dict(ev("dolt-a", "2026-05-01T10:00:00.045Z", "t-1", field="status",
                        old_value="open", new_value="closed", dolt_commit="c1"), _order=0),
                dict(ev("dolt-b", "2026-05-01T11:00:00.000Z", "t-1", field="title",
                        old_value="a", new_value="b", dolt_commit="c2"), _order=1),
                dict(ev("dolt-c", "2026-05-01T12:00:00.030Z", "t-1", field="priority",
                        old_value=2, new_value=1, dolt_commit="c3"), _order=2)]
        manifest: Dict[str, Any] = {"dropped": Counter(), "carried": Counter(field_change=3)}
        merged = dolthistory.merge(dolt, earlier, manifest)
        self.assertEqual([e["id"] for e in merged], ["int-1", "dolt-b", "int-2"])
        self.assertEqual(manifest["earlier_events_matched"], 2)
        self.assertNotIn("_order", merged[1])

    def test_a_notes_append_the_log_also_holds_is_kept_once(self):
        """Grok's review of d0eecba: Dolt writes the append as `appended`, the log as full."""
        earlier = [ev("int-1", "2026-05-01T10:00:00.010Z", "t-1", field="notes",
                      old_value="n1", new_value="n1\nn2")]
        dolt = [dict(ev("dolt-a", "2026-05-01T10:00:00.040Z", "t-1", field="notes",
                        appended="\nn2", dolt_commit="c1"), _order=0)]
        manifest: Dict[str, Any] = {"dropped": Counter(), "carried": Counter(field_change=1)}
        self.assertEqual([e["id"] for e in dolthistory.merge(dolt, earlier, manifest)], ["int-1"])


class FileTest(StoreCase):
    """The events file the verb writes is what `init --events` loads and `export --events`
    gives back, byte for byte."""

    def test_written_file_imports_and_re_exports_unchanged(self):
        events = [dict(ev("dolt-x", "2026-05-01T10:00:00.000Z", "t-aaa", field="notes",
                          old_value=None, new_value='a "q" <&>  ', dolt_commit="c"))]
        path = self.workspace / "hist.jsonl"
        dolthistory.write_events(events, path)
        export = self.workspace / "export.jsonl"
        export.write_text(json.dumps({"_type": "issue", "id": "t-aaa", "title": "x",
                                      "status": "open", "priority": 2, "issue_type": "task",
                                      "created_at": "2026-05-01T10:00:00Z",
                                      "updated_at": "2026-05-01T10:00:00Z"}) + "\n")
        store = self.workspace / "s"
        store.mkdir()
        code, _, err = self.cli("init", "-p", "t", "--from", str(export), "--events", str(path),
                                cwd=store)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli("export", "--events", cwd=store)[1], path.read_text())


if __name__ == "__main__":
    unittest.main()
