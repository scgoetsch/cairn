"""The write path, on writable stores made by `cairn init`. Real SQLite in temp
directories; concurrency through real subprocesses."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from cairn.queries import Snapshot
from cairn.store import db_path, open_db

from .helpers import StoreCase
from .test_cli import ROWS as ROWS_FOR_SHADOW

REPO = Path(__file__).resolve().parents[1]


class WritableCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("BEADS_ACTOR", None)
        code, _, err = self.cli("init", "--prefix", "t")
        self.assertEqual(code, 0, err)

    def w(self, *argv: str, actor: str = "alice") -> str:
        code, out, err = self.cli(*argv, "--actor", actor)
        self.assertEqual(code, 0, f"{argv}: {err}")
        return out.strip()

    def new(self, title: str, *argv: str, actor: str = "alice") -> str:
        return self.w("create", "--silent", "--title", title, *argv, actor=actor)

    def snap(self) -> Snapshot:
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        return Snapshot(conn)

    def row(self, issue_id: str):
        return self.snap().issues[issue_id]

    def events(self, issue_id: str) -> List[Dict[str, Any]]:
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE issue_id = ? ORDER BY rowid", (issue_id,))]

    def ready(self) -> List[str]:
        _, out, _ = self.cli("ready", "--json")
        return [i["id"] for i in json.loads(out)]


class InitTest(WritableCase):
    def test_init_twice_is_refused(self):
        code, _, err = self.cli("init", "--prefix", "t")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_a_shadow_store_refuses_every_write(self):
        other = self.workspace / "shadowws"
        other.mkdir()
        self.store_dir = other / ".cairn"
        self.build(ROWS_FOR_SHADOW)
        for argv in (["create", "--title", "x"], ["close", "t-epic"],
                     ["remember", "--key", "k", "v"], ["ready", "--claim"]):
            code, out, err = self.cli(*argv, "--actor", "a", cwd=other)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertIn("read-only shadow", err)


class IdTest(WritableCase):
    def test_root_ids_have_the_prefix_and_three_base36_characters(self):
        ids = {self.new(f"r{i}") for i in range(20)}
        self.assertEqual(len(ids), 20)
        for issue_id in ids:
            self.assertRegex(issue_id, r"^t-[0-9a-z]{3}$")

    def test_child_numbers_are_never_reissued(self):
        parent = self.new("parent")
        a, b, c = (self.new(x, "--parent", parent) for x in "abc")
        self.assertEqual([a, b, c], [f"{parent}.1", f"{parent}.2", f"{parent}.3"])
        self.w("delete", c, "--force")
        self.w("delete", b, "--force")
        self.assertEqual(self.new("d", "--parent", parent), f"{parent}.4")
        self.assertEqual(self.new("grandchild", "--parent", a), f"{a}.1")
        self.assertEqual(self.snap().parents_of[f"{parent}.4"], [parent])

    def test_create_id_of_an_existing_or_deleted_issue_is_refused(self):
        live = self.new("live")
        gone = self.new("gone")
        self.w("delete", gone, "--force")
        for taken in (live, gone):
            code, _, err = self.cli("create", "--id", taken, "--title", "x", "--actor", "a")
            self.assertEqual(code, 2)
            self.assertIn("refused", err)
        self.assertEqual(self.row(live)["title"], "live")

    def test_create_id_with_parent_or_foreign_prefix_is_refused(self):
        parent = self.new("p")
        for argv in (["--id", "t-zzz", "--parent", parent], ["--id", "x-abc"]):
            code, _, _ = self.cli("create", "--title", "x", *argv, "--actor", "a")
            self.assertEqual(code, 2, argv)

    def test_create_with_explicit_id(self):
        self.assertEqual(self.new("explicit", "--id", "t-abcd"), "t-abcd")
        self.assertRegex(self.new("next"), r"^t-[0-9a-z]{4}$")   # width follows the store


class FieldTest(WritableCase):
    def test_create_records_who_and_defaults(self):
        issue_id = self.new("x", "-d", "desc", "-p", "1", "-t", "bug", "-l", "a,b")
        row = self.row(issue_id)
        self.assertEqual((row["status"], row["priority"], row["issue_type"], row["created_by"],
                          row["description"]), ("open", 1, "bug", "alice", "desc"))
        self.assertEqual(row["created_at"], row["updated_at"])
        self.assertEqual(self.snap().labels[issue_id], ["a", "b"])
        self.assertEqual([e["kind"] for e in self.events(issue_id)], ["created"])

    def test_update_fields_append_notes_and_labels(self):
        issue_id = self.new("x")
        self.w("update", issue_id, "--title", "T2", "--design", "G", "--acceptance", "AC",
               "--notes", "N1", "-p", "P0", "-t", "feature")
        self.w("update", issue_id, "--append-notes", "N2", "--add-label", "x")
        self.w("note", issue_id, "N3")
        row = self.row(issue_id)
        self.assertEqual((row["title"], row["design"], row["acceptance_criteria"], row["notes"],
                          row["priority"], row["issue_type"]),
                         ("T2", "G", "AC", "N1\nN2\nN3", 0, "feature"))
        fields = [json.loads(e["data"]).get("field") for e in self.events(issue_id)
                  if e["kind"] == "field_change"]
        self.assertEqual(fields.count("notes"), 3)
        self.assertIn("title", fields)

    def test_an_update_that_changes_nothing_leaves_no_event(self):
        issue_id = self.new("x")
        self.w("update", issue_id, "--title", "x")
        self.assertEqual([e["kind"] for e in self.events(issue_id)], ["created"])

    def test_close_reopen_follow_bd(self):
        issue_id = self.new("x")
        self.w("update", issue_id, "--claim")
        started = self.row(issue_id)["started_at"]
        self.w("close", issue_id)
        row = self.row(issue_id)
        self.assertEqual((row["status"], row["close_reason"], row["assignee"]),
                         ("closed", "Closed", "alice"))
        self.assertIsNotNone(row["closed_at"])
        self.w("reopen", issue_id)
        row = self.row(issue_id)
        self.assertEqual((row["status"], row["closed_at"], row["close_reason"],
                          row["started_at"], row["assignee"]),
                         ("open", None, None, started, "alice"))
        self.w("close", issue_id, "--reason", "done it")
        self.assertEqual(self.row(issue_id)["close_reason"], "done it")
        status_event = [json.loads(e["data"]) for e in self.events(issue_id)
                        if e["kind"] == "field_change"
                        and json.loads(e["data"])["field"] == "status"][-1]
        self.assertEqual(status_event, {"field": "status", "old_value": "open",
                                        "new_value": "closed", "reason": "done it"})

    def test_status_blocked_is_refused(self):
        issue_id = self.new("x")
        for argv in (["update", issue_id, "--status", "blocked"],
                     ["update", issue_id, "-s", "nonsense"]):
            code, _, err = self.cli(*argv, "--actor", "a")
            self.assertEqual(code, 2, argv)
        self.assertEqual(self.row(issue_id)["status"], "open")

    def test_defer_and_undefer(self):
        issue_id = self.new("x")
        self.w("defer", issue_id, "--until", "2099-01-01")
        row = self.row(issue_id)
        self.assertEqual((row["status"], row["defer_until"]), ("deferred", "2099-01-01T00:00:00Z"))
        self.assertNotIn(issue_id, self.ready())
        self.w("undefer", issue_id)
        row = self.row(issue_id)
        self.assertEqual((row["status"], row["defer_until"]), ("open", None))
        self.assertIn(issue_id, self.ready())

    def test_comment(self):
        issue_id = self.new("x")
        self.w("comment", issue_id, "first")
        self.w("comments", "add", issue_id, "second")
        _, out, _ = self.cli("show", issue_id, "--json")
        comments = json.loads(out)[0]["comments"]
        self.assertEqual([(c["author"], c["text"]) for c in comments],
                         [("alice", "first"), ("alice", "second")])


class ClaimTest(WritableCase):
    def test_compare_and_swap(self):
        issue_id = self.new("x")
        self.w("update", issue_id, "--claim", actor="A")
        row = self.row(issue_id)
        self.assertEqual((row["assignee"], row["status"]), ("A", "in_progress"))
        code, _, err = self.cli("update", issue_id, "--claim", "--actor", "B")
        self.assertEqual(code, 1)
        self.assertIn("already claimed by A", err)
        self.assertEqual(self.row(issue_id)["assignee"], "A")
        self.w("update", issue_id, "--claim", actor="A")                 # same actor: fine
        self.w("update", issue_id, "--assignee", "B", actor="B")         # takeover, logged
        self.w("update", issue_id, "--claim", actor="B")
        self.assertEqual(self.row(issue_id)["assignee"], "B")
        changes = [json.loads(e["data"]) for e in self.events(issue_id)
                   if e["kind"] == "field_change" and json.loads(e["data"])["field"] == "assignee"]
        self.assertEqual([(c["old_value"], c["new_value"]) for c in changes],
                         [(None, "A"), ("A", "B")])

    def test_closed_is_not_claimable(self):
        issue_id = self.new("x")
        self.w("close", issue_id)
        code, _, err = self.cli("update", issue_id, "--claim", "--actor", "A")
        self.assertEqual(code, 1)
        self.assertIn("not claimable: status closed", err)

    def test_claim_without_an_actor_is_refused(self):
        issue_id = self.new("x")
        code, _, err = self.cli("update", issue_id, "--claim")
        self.assertEqual(code, 2)
        self.assertIn("needs an actor", err)
        self.assertEqual(self.row(issue_id)["assignee"], None)

    def test_missing_issue(self):
        code, _, err = self.cli("update", "t-nope", "--claim", "--actor", "A")
        self.assertEqual(code, 1)
        self.assertIn("no issue found", err)

    def test_ready_claim_takes_the_first_ready_issue(self):
        first = self.new("urgent", "-p", "0")
        self.new("later", "-p", "3")
        code, out, err = self.cli("ready", "--claim", "--json", "--actor", "A")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["id"], first)
        self.assertEqual(self.row(first)["assignee"], "A")

    def test_parallel_claims_one_winner(self):
        issue_id = self.new("contended")
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        procs = [subprocess.Popen(
            [sys.executable, "-m", "cairn", "-C", str(self.workspace), "update", issue_id,
             "--claim", "--actor", f"s{i}"], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True) for i in range(8)]
        results = [(p.wait(timeout=60), p.stderr.read()) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        winners = [i for i, (code, _) in enumerate(results) if code == 0]
        self.assertEqual(len(winners), 1, results)
        for code, err in results:
            self.assertNotIn("locked", err)
            if code != 0:
                self.assertEqual(code, 1, err)
                self.assertIn("already claimed by", err)
        self.assertEqual(self.row(issue_id)["assignee"], f"s{winners[0]}")

    def test_parallel_creates_distinct_ids(self):
        parent = self.new("p")
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        procs = [subprocess.Popen(
            [sys.executable, "-m", "cairn", "-C", str(self.workspace), "create", "--silent",
             "--title", f"c{i}", "--actor", "a", *(["--parent", parent] if i % 2 else [])],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(10)]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            outs.append(out.strip())
        self.assertEqual(len(set(outs)), 10)
        children = sorted(o for o in outs if o.startswith(parent + "."))
        self.assertEqual(sorted(children, key=lambda x: int(x.rsplit(".", 1)[1])),
                         [f"{parent}.{n}" for n in range(1, 6)])


class DependencyTest(WritableCase):
    def test_blocker_hides_and_returns(self):
        issue_id = self.new("work")
        blocker = self.new("blocker")
        self.w("dep", "add", issue_id, blocker)
        self.assertNotIn(issue_id, self.ready())
        self.w("close", blocker)
        self.assertIn(issue_id, self.ready())
        self.assertEqual(self.snap().deps_of[issue_id][0]["created_by"], "alice")

    def test_second_parent_is_refused(self):
        a, b = self.new("a"), self.new("b")
        kid = self.new("kid", "--parent", a)
        code, _, err = self.cli("dep", "add", kid, b, "--type", "parent-child", "--actor", "x")
        self.assertEqual(code, 2)
        self.assertIn("already has a parent", err)

    def test_a_blocks_cycle_is_refused_as_bd_refuses_it(self):
        # bd 1.1.2 and 1.3.0, measured (workspace-7m8.19): exit 1, "adding
        # dependency would create a cycle", through any number of blocks edges.
        a, b, c = self.new("a"), self.new("b"), self.new("c")
        self.w("dep", "add", a, b)
        self.w("dep", "add", b, c)
        code, _, err = self.cli("dep", "add", c, a, "--actor", "x")
        self.assertEqual(code, 1)
        self.assertIn("adding dependency would create a cycle", err)
        self.assertIn(f"{c} -> {a} -> {b} -> {c}", err)
        self.assertEqual(len(self.snap().deps), 2)
        self.assertEqual([e["kind"] for e in self.events(c)], ["created"])
        self.assertIn(c, self.ready())

    def test_a_closed_issue_on_the_path_still_closes_the_cycle(self):
        a, b = self.new("a"), self.new("b")
        self.w("dep", "add", a, b)
        self.w("close", b)
        code, _, err = self.cli("dep", "add", b, a, "--actor", "x")
        self.assertEqual(code, 1, err)

    def test_only_blocks_edges_make_a_cycle(self):
        # bd accepts each of these (measured): the walk follows blocks edges only.
        p, k = self.new("p"), self.new("k")
        self.w("dep", "add", k, p, "--type", "parent-child")
        self.w("dep", "add", p, k)
        x, y = self.new("x"), self.new("y")
        self.w("dep", "add", x, y)
        self.w("dep", "add", y, x, "--type", "related")
        self.w("dep", "add", y, x, "--type", "discovered-from")
        code, out, _ = self.cli("check", "--json")
        self.assertEqual(code, 0, out)

    def test_check_names_a_blocks_cycle_already_in_the_store(self):
        # A store written before dep add refused cycles can hold one.
        a, b, c, d = self.new("a"), self.new("b"), self.new("c"), self.new("d")
        self.w("dep", "add", a, b)
        self.w("dep", "add", b, c)
        self.w("dep", "add", d, a)
        conn = sqlite3.connect(db_path(self.store_dir))      # a writable connection
        self.addCleanup(conn.close)
        with conn:
            conn.execute("INSERT INTO dependencies(issue_id, depends_on_id, type, created_at, "
                         "created_by) VALUES (?, ?, 'blocks', '2026-10-19T00:00:00Z', 'old')",
                         (c, a))
        code, out, _ = self.cli("check", "--json")
        self.assertEqual(code, 1)
        cycles = [e for e in json.loads(out)["errors"] if e.startswith("blocks cycle")]
        self.assertEqual(cycles, [f"blocks cycle: {', '.join(sorted([a, b, c]))} "
                                  "(none of them can become ready)"])

    def test_dep_remove_takes_every_type(self):
        a, b = self.new("a"), self.new("b")
        self.w("dep", "add", a, b)
        self.w("dep", "add", a, b, "--type", "related")
        self.w("dep", "rm", a, b)
        self.assertNotIn(a, self.snap().deps_of)

    def test_self_and_unknown(self):
        a = self.new("a")
        for argv, want in ((["dep", "add", a, a], 2), (["dep", "add", a, "t-nope"], 1),
                           (["dep", "add", a, a, "--type", "frobs"], 2)):
            code, _, _ = self.cli(*argv, "--actor", "x")
            self.assertEqual(code, want, argv)

    def test_delete_removes_edges_both_ways(self):
        a, b, c = self.new("a"), self.new("b"), self.new("c")
        self.w("dep", "add", a, b)
        self.w("dep", "add", b, c)
        out = self.w("delete", b, "--force")
        self.assertIn("removed 2", out)
        snap = self.snap()
        self.assertNotIn(b, snap.issues)
        self.assertEqual(snap.deps, [])

    def test_delete_needs_force(self):
        a = self.new("a")
        code, _, _ = self.cli("delete", a, "--actor", "x")
        self.assertEqual(code, 2)
        self.assertIn(a, self.snap().issues)


class MemoryTest(WritableCase):
    def mem(self):
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        return conn

    def test_remember_update_forget_and_edges(self):
        issue_id = self.new("x")
        self.w("remember", "--key", "mem-one", f"see [[mem-two]] and [[{issue_id}]]")
        conn = self.mem()
        self.assertEqual(sorted(tuple(r) for r in conn.execute(
            "SELECT source_key, target, target_kind FROM memory_edges")),
            [("mem-one", "mem-two", "memory"), ("mem-one", issue_id, "issue")])
        out = self.w("remember", "--key", "mem-one", "supersedes::[[mem-three]]")
        self.assertTrue(out.startswith("Updated [mem-one]"))
        conn = self.mem()
        self.assertEqual([tuple(r) for r in conn.execute(
            "SELECT source_key, target, relation FROM memory_edges")],
            [("mem-one", "mem-three", "supersedes")])
        self.w("forget", "mem-one")
        conn = self.mem()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT key FROM memory_tombstones").fetchone()[0], "mem-one")
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM events WHERE memory_key = 'mem-one' ORDER BY rowid")]
        self.assertEqual(kinds, ["memory_set", "memory_set", "memory_deleted"])

    def test_remember_without_key_is_refused(self):
        code, _, err = self.cli("remember", "body", "--actor", "x")
        self.assertEqual(code, 2)
        self.assertIn("--key", err)

    def test_forget_missing(self):
        code, _, _ = self.cli("forget", "nope", "--actor", "x")
        self.assertEqual(code, 1)


class AdmissionTest(WritableCase):
    """remember carries bd-memgraph's evolve (workspace-7m8.20)."""
    BODY = ("sov2 scale runs: cairn ready latency stays under one second at thirty "
            "thousand issues while bd times out under eight workers")

    def test_a_near_duplicate_of_another_key_is_refused_and_names_it(self):
        self.w("remember", "--key", "scale-results", self.BODY)
        code, _, err = self.cli("remember", "--key", "scale-again", self.BODY + " today",
                                "--actor", "x")
        self.assertEqual(code, 1)
        self.assertIn("near-duplicate of memory 'scale-results'", err)
        self.assertIn("cairn recall scale-results", err)
        code, _, _ = self.cli("recall", "scale-again")
        self.assertEqual(code, 1)

    def test_force_stores_it_anyway(self):
        self.w("remember", "--key", "scale-results", self.BODY)
        self.w("remember", "--key", "scale-again", self.BODY, "--force")
        _, out, _ = self.cli("recall", "scale-again")
        self.assertEqual(out.strip(), self.BODY)

    def test_rewriting_the_same_key_is_an_update_not_a_duplicate(self):
        self.w("remember", "--key", "scale-results", self.BODY)
        out = self.w("remember", "--key", "scale-results", self.BODY + " (checked twice)")
        self.assertIn("Updated [scale-results]", out)

    def test_a_body_that_supersedes_the_key_it_resembles_is_admitted(self):
        self.w("remember", "--key", "scale-results", self.BODY)
        self.w("remember", "--key", "scale-results-v2",
               self.BODY + " supersedes::[[scale-results]]")

    def test_a_related_body_without_links_gets_the_keys_to_link(self):
        self.w("remember", "--key", "scale-results", self.BODY)
        draft = ("agent sessions on sov2 measured ready latency against a budget of two "
                 "seconds per verb, reported in the evaluation")
        code, _, err = self.cli("remember", "--key", "latency-note", draft, "--actor", "x")
        self.assertEqual(code, 0, err)
        self.assertIn("[[scale-results]]", err)
        code, _, err = self.cli("remember", "--key", "latency-note",
                                draft + " see [[scale-results]]", "--actor", "x")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.strip(), "")

    def test_a_standalone_body_says_nothing(self):
        self.w("remember", "--key", "scale-results", self.BODY)
        code, _, err = self.cli("remember", "--key", "tea", "green tea steeps two minutes",
                                "--actor", "x")
        self.assertEqual((code, err.strip()), (0, ""))


class TransactionTest(WritableCase):
    def test_a_failing_write_leaves_neither_change_nor_event(self):
        from cairn.store import open_writable
        from cairn.writer import Writer
        issue_id = self.new("x")
        conn = open_writable(db_path(self.store_dir))
        self.addCleanup(conn.close)
        writer = Writer(conn, actor="a")
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON events "
                     "WHEN NEW.kind = 'field_change' BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with self.assertRaises(Exception):
            writer.update(issue_id, title="changed")
        self.assertEqual(self.row(issue_id)["title"], "x")

    def test_events_stay_append_only(self):
        import sqlite3
        from cairn.store import open_writable
        issue_id = self.new("x")
        conn = open_writable(db_path(self.store_dir))
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM events WHERE issue_id = ?", (issue_id,))
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE events SET data = '{}' WHERE issue_id = ?", (issue_id,))

    def test_writable_store_is_in_wal_mode(self):
        self.new("x")
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class ReviewFindingsTest(WritableCase):
    """Defects found by the 2026-10-15 peer review (grok + agy) of f86445c."""

    def count_events(self) -> int:
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def test_shadow_sync_and_import_refuse_to_replace_a_writable_store(self):
        keep = self.new("keep me")
        export = self.workspace / "x.jsonl"
        export.write_text(json.dumps({"_type": "issue", "id": "t-new", "title": "n",
                                      "status": "open", "priority": 2, "issue_type": "task",
                                      "created_at": "2026-01-01T00:00:00Z",
                                      "updated_at": "2026-01-01T00:00:00Z"}) + "\n")
        code, _, err = self.cli("import", str(export))
        self.assertEqual(code, 2)
        self.assertIn("writable cairn store", err)
        from cairn.importer import build_store
        from cairn.store import StoreError
        with self.assertRaises(StoreError):
            build_store(db_path(self.store_dir), export)     # the path shadow sync takes
        self.assertIn(keep, self.snap().issues)

    def test_defer_is_validated_before_anything_is_written(self):
        code, _, err = self.cli("create", "--title", "x", "--defer", "next-friday", "--actor", "a")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.snap().issues, {})
        issue_id = self.new("y")
        before = self.count_events()
        code, _, _ = self.cli("update", issue_id, "--title", "z", "--defer", "soon", "--actor", "a")
        self.assertEqual(code, 2)
        self.assertEqual((self.row(issue_id)["title"], self.count_events()), ("y", before))

    def test_relative_defer_dates(self):
        issue_id = self.new("x", "--defer", "+30d")
        row = self.row(issue_id)
        self.assertEqual(row["status"], "deferred")
        self.assertRegex(row["defer_until"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        for when in ("+2w", "+3h", "tomorrow", "2027-02-03"):
            self.w("defer", issue_id, "--until", when)

    def test_closing_a_closed_issue_changes_nothing(self):
        issue_id = self.new("x")
        self.w("close", issue_id, "--reason", "one")
        before, closed_at = self.count_events(), self.row(issue_id)["closed_at"]
        out = self.w("close", issue_id, "--reason", "two")
        self.assertIn("already closed", out)
        row = self.row(issue_id)
        self.assertEqual((row["close_reason"], row["closed_at"], self.count_events()),
                         ("one", closed_at, before))

    def test_close_with_an_open_blocker_needs_force(self):
        work, blocker = self.new("work"), self.new("blocker")
        self.w("dep", "add", work, blocker)
        code, _, err = self.cli("close", work, "--actor", "a")
        self.assertEqual(code, 1)
        self.assertIn(f"blocked by open issues [{blocker}]", err)
        self.assertEqual(self.row(work)["status"], "open")
        self.w("close", work, "--force")
        self.assertEqual(self.row(work)["status"], "closed")

    def test_a_deferred_issue_is_not_claimable(self):
        issue_id = self.new("x", "--defer", "+1w")
        code, _, err = self.cli("update", issue_id, "--claim", "--actor", "A")
        self.assertEqual(code, 1)
        self.assertIn("not claimable: status deferred", err)

    def test_every_column_change_has_an_event(self):
        issue_id = self.new("x")
        self.w("update", issue_id, "--claim", actor="A")
        self.w("close", issue_id, "--reason", "r", actor="A")
        fields = {json.loads(e["data"])["field"] for e in self.events(issue_id)
                  if e["kind"] == "field_change"}
        self.assertTrue({"assignee", "status", "started_at", "closed_at", "close_reason"} <= fields,
                        fields)

    def test_reclaim_by_the_holder_writes_nothing(self):
        issue_id = self.new("x")
        self.w("update", issue_id, "--claim", actor="A")
        before = self.count_events()
        self.w("update", issue_id, "--claim", actor="A")
        self.assertEqual(self.count_events(), before)

    def test_delete_records_survivors_and_the_whole_record(self):
        a, b = self.new("a"), self.new("b", "-l", "keep")
        self.w("dep", "add", a, b)
        self.w("comment", b, "a comment")
        self.w("delete", b, "--force")
        lost = [json.loads(e["data"]) for e in self.events(a) if e["kind"] == "dependency_removed"]
        self.assertEqual(lost, [{"depends_on_id": b, "type": "blocks", "reason": "target deleted"}])
        gone = [json.loads(e["data"]) for e in self.events(b) if e["kind"] == "deleted"][0]
        self.assertEqual((gone["issue"]["title"], gone["labels"], gone["comments"][0]["text"]),
                         ("b", ["keep"], "a comment"))

    def test_an_explicit_child_shaped_id_moves_the_counter(self):
        parent = self.new("p")
        self.new("explicit", "--id", f"{parent}.5")
        self.assertEqual(self.new("next", "--parent", parent), f"{parent}.6")

    def test_ready_claim_uses_the_ready_filters(self):
        self.new("urgent", "-p", "0")
        low = self.new("low", "-p", "3")
        code, out, err = self.cli("ready", "--claim", "-p", "3", "--json", "--actor", "A")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["id"], low)

    def test_ready_claim_with_everything_held_elsewhere(self):
        issue_id = self.new("x")
        self.w("update", issue_id, "--assignee", "B")
        code, out, _ = self.cli("ready", "--claim", "--json", "--actor", "A")
        self.assertEqual((code, json.loads(out)), (1, []))

    def test_issue_prefixes_from_the_environment_apply_to_writes(self):
        os.environ["CAIRN_ISSUE_PREFIXES"] = "other"
        self.addCleanup(os.environ.pop, "CAIRN_ISSUE_PREFIXES", None)
        self.w("remember", "--key", "mem-one", "see [[other-abc]]")
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT target_kind FROM memory_edges").fetchone()[0], "issue")

    def test_json_on_every_write(self):
        a, b = self.new("a"), self.new("b")
        for argv in (["dep", "add", a, b], ["dep", "rm", a, b],
                     ["remember", "--key", "mem-one", "v"], ["forget", "mem-one"],
                     ["delete", b, "--force"]):
            code, out, err = self.cli("--json", *argv, "--actor", "x")
            self.assertEqual(code, 0, f"{argv}: {err}")
            json.loads(out)
