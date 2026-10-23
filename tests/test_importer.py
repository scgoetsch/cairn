from __future__ import annotations

import json
import sqlite3

from cairn.importer import SourceError, build_store
from cairn.queries import Snapshot
from cairn.store import db_path

from .helpers import StoreCase, child, dep, issue, memory, with_deps, write_jsonl

FULL = {
    "_type": "issue", "id": "t-full", "title": "Full issue; with \"quotes\" and\nnewline",
    "description": "desc — unicode ✓", "design": "design text",
    "acceptance_criteria": "must pass", "notes": "a note",
    "status": "closed", "priority": 1, "issue_type": "bug",
    "assignee": "ann", "owner": "ann@example.org",
    "created_at": "2026-09-01T10:00:00Z", "created_by": "ann",
    "updated_at": "2026-09-02T10:00:00Z", "started_at": "2026-09-01T11:00:00Z",
    "closed_at": "2026-09-02T10:00:00Z", "close_reason": "fixed",
    "comments": [{"id": "c1", "issue_id": "t-full", "author": "ann",
                  "text": "a comment", "created_at": "2026-09-01T12:00:00Z"}],
    "labels": ["alpha", "beta"],
    "dependencies": [dep("t-full", "t-other")],
    "some_future_field": {"kept": True},
    "dependency_count": 1, "dependent_count": 0, "comment_count": 1,
}


class RoundTripTest(StoreCase):
    def test_export_reproduces_every_field_of_the_source(self):
        rows = [FULL, issue("t-other", dependent_count=1), child("t-other.1", "t-other"),
                memory("k-one", "body one"), memory("k-two", "body [[k-one]]")]
        self.build(rows)
        code, out, _ = self.cli("export", "--all")
        self.assertEqual(code, 0)
        exported = {(r["_type"], r.get("id") or r.get("key")): r
                    for r in map(json.loads, out.splitlines())}
        source = {(r["_type"], r.get("id") or r.get("key")): r for r in rows}
        self.assertEqual(set(exported), set(source))
        for key, row in source.items():
            want = dict(row)
            if row["_type"] == "issue":
                for counter in ("dependency_count", "dependent_count", "comment_count"):
                    want.setdefault(counter, 0)
            self.assertEqual(exported[key], want, key)

    def test_export_is_sorted_and_stable(self):
        self.build([issue("t-b"), issue("t-a"), memory("k-z", "z"), memory("k-a", "a")])
        _, first, _ = self.cli("export", "--all")
        _, second, _ = self.cli("export", "--all")
        self.assertEqual(first, second)
        ids = [json.loads(line).get("id") or json.loads(line)["key"]
               for line in first.splitlines()]
        self.assertEqual(ids, ["t-a", "t-b", "k-a", "k-z"])

    def test_memories_are_left_out_of_export_by_default(self):
        self.build([issue("t-a"), memory("k-a", "a")])
        _, out, _ = self.cli("export")
        self.assertEqual([json.loads(line)["_type"] for line in out.splitlines()], ["issue"])

    def test_events_come_from_the_interactions_log(self):
        log = write_jsonl(self.workspace / "interactions.jsonl", [{
            "id": "int-1", "kind": "field_change", "created_at": "2026-09-01T10:00:00.5Z",
            "actor": "ann", "issue_id": "t-a",
            "extra": {"field": "status", "old_value": "open", "new_value": "closed"}}])
        conn, report = self.build([issue("t-a")], interactions_path=log)
        self.assertEqual(report.counts["events"], 1)
        row = conn.execute("SELECT * FROM events").fetchone()
        self.assertEqual(row["issue_id"], "t-a")
        self.assertEqual(json.loads(row["data"])["new_value"], "closed")


class RejectTest(StoreCase):
    """Bad input is an error that names the file and line — never a silent skip."""

    def build_raw(self, text: str):
        export = self.workspace / "export.jsonl"
        export.write_text(text, encoding="utf-8")
        return build_store(db_path(self.store_dir), export)

    def assert_rejected(self, rows_or_text, fragment: str):
        text = rows_or_text if isinstance(rows_or_text, str) else "".join(
            json.dumps(r) + "\n" for r in rows_or_text)
        with self.assertRaises(SourceError) as caught:
            self.build_raw(text)
        self.assertIn(fragment, str(caught.exception))
        self.assertIn("export.jsonl:", str(caught.exception))

    def test_truncated_json_line(self):
        self.assert_rejected(json.dumps(issue("t-a")) + "\n" + '{"_type": "issue", "id": "t-b"\n',
                             "export.jsonl:2: not valid JSON")

    def test_non_object_line(self):
        self.assert_rejected('["not", "an", "object"]\n', "expected a JSON object")

    def test_unknown_status(self):
        self.assert_rejected([issue("t-a", status="wontfix")], "unknown status 'wontfix'")

    def test_priority_out_of_range_or_wrong_type(self):
        self.assert_rejected([issue("t-a", priority=7)], "priority 7")
        self.assert_rejected([issue("t-a", priority="2")], "priority '2'")
        self.assert_rejected([issue("t-a", priority=True)], "priority True")

    def test_missing_required_field(self):
        row = issue("t-a")
        del row["title"]
        self.assert_rejected([row], "missing 'title'")

    def test_duplicate_issue_id(self):
        self.assert_rejected([issue("t-a"), issue("t-a")], "duplicate issue id t-a")

    def test_duplicate_memory_key(self):
        self.assert_rejected([memory("k", "one"), memory("k", "two")], "duplicate memory key k")

    def test_memory_without_a_string_value(self):
        self.assert_rejected([{"_type": "memory", "key": "k", "value": 3}], "no string 'value'")

    def test_values_sqlite_cannot_bind_are_named_not_crashed_on(self):
        self.assert_rejected([issue("t-a", title=["a", "list"])],
                             "issue t-a: 'title' must be a string, got list")
        self.assert_rejected([issue("t-a", notes={"k": "v"})],
                             "issue t-a: 'notes' must be a string, got dict")
        row = with_deps(issue("t-a"), dep("t-a", "t-b"))
        row["dependencies"][0]["metadata"] = {"k": "v"}
        self.assert_rejected([issue("t-b"), row],
                             "dependency of t-a: 'metadata' must be a string, got dict")
        self.assert_rejected([issue("t-a", labels="not-a-list")],
                             "'labels' must be a list of strings")
        self.assert_rejected([issue("t-a", comments=[{"id": "c1", "created_at": "x"}])],
                             "comment: missing or empty 'text'")
        self.assert_rejected([issue("t-a", dependencies="nope")],
                             "'dependencies' must be a list of objects")

    def test_timestamps_must_parse(self):
        self.assert_rejected([issue("t-a", created_at="yesterday")],
                             "'created_at' is not a timestamp: 'yesterday'")
        self.assert_rejected([issue("t-a", defer_until="2026-13-45T00:00:00Z")],
                             "'defer_until' is not a timestamp")

    def test_dependency_filed_under_the_wrong_issue(self):
        self.assert_rejected([issue("t-b"), with_deps(issue("t-a"), dep("t-b", "t-a"))],
                             "belongs to t-b")

    def test_failed_import_leaves_the_previous_store_untouched(self):
        self.build([issue("t-keep")])
        with self.assertRaises(SourceError):
            self.build_raw(json.dumps(issue("t-new", status="bogus")) + "\n")
        conn = sqlite3.connect(db_path(self.store_dir))
        self.addCleanup(conn.close)
        self.assertEqual([r[0] for r in conn.execute("SELECT id FROM issues")], ["t-keep"])
        self.assertEqual([p.name for p in self.store_dir.iterdir()], ["cairn.db"])

    def test_import_of_a_missing_file_is_a_clean_error(self):
        code, out, err = self.cli("import", str(self.workspace / "nope.jsonl"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no such file", err)
        self.assertFalse(self.store_dir.exists())


class WarningTest(StoreCase):
    def test_unsupported_row_type_is_reported_not_dropped_silently(self):
        _, report = self.build([issue("t-a"), {"_type": "gate", "id": "g-1"}])
        self.assertEqual(report.losses,
                         ["1 row(s) of unsupported _type 'gate' were not imported"])
        self.assertEqual(report.warnings, [])
        code, out, _ = self.cli("import", str(self.workspace / "export.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("✗ 1 row(s) of unsupported _type 'gate'", out)

    def test_dangling_dependency_is_kept_and_reported(self):
        conn, report = self.build([with_deps(issue("t-a"), dep("t-a", "t-missing"))])
        self.assertIn("dangling dependency: t-a -blocks-> t-missing", report.warnings[0])
        self.assertEqual(len(Snapshot(conn).deps), 1)
        code, out, _ = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("dangling dependency: t-a -blocks-> t-missing", out)


class HistoryTest(StoreCase):
    def test_events_are_append_only(self):
        log = write_jsonl(self.workspace / "interactions.jsonl", [{
            "id": "int-1", "kind": "field_change", "created_at": "2026-09-01T10:00:00Z",
            "actor": "ann", "issue_id": "t-a", "extra": {}}])
        self.build([issue("t-a")], interactions_path=log)
        conn = sqlite3.connect(db_path(self.store_dir))      # a writable connection
        self.addCleanup(conn.close)
        for statement in ("UPDATE events SET actor = 'mallory'", "DELETE FROM events"):
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                conn.execute(statement)
            self.assertIn("append-only", str(caught.exception))
        self.assertEqual(conn.execute("SELECT actor FROM events").fetchone()[0], "ann")

    def test_read_verbs_open_the_store_read_only(self):
        from cairn.store import open_db
        self.build([issue("t-a")])
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("UPDATE issues SET title = 'changed'")


class StatsTest(StoreCase):
    def test_count_refuses_a_table_outside_the_allowlist(self):
        from cairn.queries import _count
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError):
            _count(conn, "issues; DROP TABLE issues")
