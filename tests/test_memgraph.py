"""The memory graph under cairn (workspace-7m8.5.4): `memories --rank` and `related`, ported
from bd-memgraph; and what a rebuild from the four tracked files takes back from the memory
events (gap-closure spec, section 4: tombstones; workspace-7m8.5.9: created_at/updated_at)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest import mock

from cairn import memgraph
from cairn.store import db_path

from .test_sync import SyncCase
from .test_writer import WritableCase


def rows(clone: Path, sql: str, *params):
    conn = sqlite3.connect(db_path(clone / ".cairn"))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


class ReplayTest(SyncCase):
    def rebuild_b(self, *argv: str):
        self.refresh(self.a)
        self.push(self.a, self.b)
        return self.cli("sync", *argv, cwd=self.b)

    def test_tombstone_comes_back_from_the_events(self):
        self.w(self.a, "remember", "--key", "mem-keep", "see [[mem-gone]]")
        self.w(self.a, "remember", "--key", "mem-gone", "soon gone")
        self.w(self.a, "forget", "mem-gone")
        code, _, err = self.rebuild_b()
        self.assertEqual(code, 0, err)
        events = {e["kind"]: e for e in rows(self.b, "SELECT kind, created_at FROM events "
                                              "WHERE memory_key = 'mem-gone'")}
        stone = rows(self.b, "SELECT first_seen, deleted_at FROM memory_tombstones "
                             "WHERE key = 'mem-gone'")
        self.assertEqual(stone, [{"first_seen": events["memory_set"]["created_at"],
                                  "deleted_at": events["memory_deleted"]["created_at"]}])
        code, out, _ = self.cli("check", cwd=self.b)
        self.assertEqual(code, 1)
        self.assertIn("memory mem-keep -related-> mem-gone (deleted "
                      f"{events['memory_deleted']['created_at']})", out)

    def test_remembered_again_is_no_tombstone(self):
        self.w(self.a, "remember", "--key", "k2", "one")
        self.w(self.a, "forget", "k2")
        self.w(self.a, "remember", "--key", "k2", "back")
        self.assertEqual(self.rebuild_b()[0], 0)
        self.assertEqual(rows(self.b, "SELECT key FROM memory_tombstones"), [])

    def test_timestamps_come_back_from_the_events(self):
        self.w(self.a, "remember", "--key", "k2", "one")
        self.w(self.a, "remember", "--key", "k2", "two")
        time.sleep(1.1)                       # a later second: the same body again is no change
        self.w(self.a, "remember", "--key", "k2", "two")
        self.assertEqual(self.rebuild_b()[0], 0)
        sql = "SELECT key, created_at, updated_at FROM memories ORDER BY key"
        self.assertEqual(rows(self.b, sql), rows(self.a, sql))
        self.assertTrue(all(r["updated_at"] for r in rows(self.b, sql)))

    def test_a_live_key_whose_last_event_is_a_delete_warns_after_synced(self):
        self.w(self.a, "remember", "--key", "k2", "kept")
        self.refresh(self.a)
        self.push(self.a, self.b)
        events = self.b / ".beads" / "events.jsonl"
        last = json.loads(events.read_text().splitlines()[-1])
        forged = dict(last, id="ev-forged", kind="memory_deleted", memory_key="k2")
        with events.open("a", encoding="utf-8") as out:
            out.write(json.dumps(forged) + "\n")
        warning = ("⚠ memory k2 is in the export and its later history event is memory_deleted; "
                   "keeping the export's copy, no tombstone")
        for argv, check in ((("--json",), "json"), (("--quiet",), "quiet"), ((), "text")):
            for f in (self.b / ".cairn").glob("cairn.db*"):
                f.unlink()
            code, out, err = self.cli("sync", *argv, cwd=self.b)
            self.assertEqual(code, 0, err)
            if check == "json":
                self.assertIn(warning[2:], json.loads(out)["warnings"])
            elif check == "quiet":
                self.assertEqual(out, "")
            else:
                self.assertTrue(out.startswith("Synced "), out)
                self.assertIn("\n" + warning + "\n", out)
        self.assertEqual(rows(self.b, "SELECT key FROM memories WHERE key = 'k2'"), [{"key": "k2"}])
        self.assertEqual(rows(self.b, "SELECT key FROM memory_tombstones"), [])

    def test_events_win_over_the_ledger_and_the_ledger_fills_the_rest(self):
        self.w(self.a, "remember", "--key", "k2", "one")
        self.w(self.a, "forget", "k2")
        self.refresh(self.a)
        ledger = self.workspace / "ledger.json"
        ledger.write_text(json.dumps({
            "k2": {"first_seen": "2026-01-01", "deleted_at": "2026-01-02"},
            "bd-era": {"first_seen": "2026-02-01", "deleted_at": "2026-02-02"}}))
        export = self.workspace / "all.jsonl"
        export.write_text((self.a / ".beads" / "issues.jsonl").read_text()
                          + (self.a / ".beads" / "memories.jsonl").read_text())
        c = self.workspace / "c"
        c.mkdir()
        self.ok(c, "init", "--prefix", "t", "--from", str(export), "--ledger", str(ledger),
                "--events", str(self.a / ".beads" / "events.jsonl"), "--actor", "alice")
        stones = {r["key"]: r for r in rows(c, "SELECT * FROM memory_tombstones")}
        deleted = rows(c, "SELECT created_at FROM events WHERE kind = 'memory_deleted'")[0]
        self.assertEqual(stones["k2"]["deleted_at"], deleted["created_at"])
        self.assertEqual(stones["bd-era"]["deleted_at"], "2026-02-02")


class HandBuiltReplayTest(WritableCase):
    """Event shapes the writer no longer makes (0.4.0 hashes only, Dolt imports) and sequences it
    cannot make, fed to `init --from --events` (gap-closure spec section 4; grok's review)."""

    def event(self, n, kind, key, at, **data):
        return {"_type": "event", "id": f"ev-{n}", "kind": kind, "issue_id": None,
                "memory_key": key, "actor": "x", "created_at": at, "data": data}

    def init(self, memories, events, ledger=None):
        root = self.workspace / f"case-{len(list(self.workspace.glob('case-*')))}"
        root.mkdir()
        export = root / "export.jsonl"
        export.write_text("".join(json.dumps({"_type": "memory", "key": k, "value": v}) + "\n"
                                  for k, v in memories.items()))
        ev = root / "events.jsonl"
        ev.write_text("".join(json.dumps(e) + "\n" for e in events))
        argv = ["init", "--prefix", "t", "--from", str(export), "--events", str(ev),
                "--actor", "alice"]
        if ledger is not None:
            (root / "ledger.json").write_text(json.dumps(ledger))
            argv += ["--ledger", str(root / "ledger.json")]
        code, out, err = self.cli(*argv, cwd=root)
        self.assertEqual(code, 0, err)
        return root, out + err

    def test_old_shapes_date_a_live_memory(self):
        root, _ = self.init({"k": "v3"}, [
            self.event(1, "memory_set", "k", "2026-08-01T10:00:00.123Z", dolt_commit="c1",
                       old_hash=None, new_hash="h1", old_value=None),
            self.event(2, "memory_set", "k", "2026-09-01T10:00:00Z", old_hash="h1", new_hash="h2"),
            self.event(3, "memory_set", "k", "2026-10-01T10:00:00Z", old_hash="h2", new_hash="h2")])
        self.assertEqual(rows(root, "SELECT created_at, updated_at FROM memories"),
                         [{"created_at": "2026-08-01T10:00:00.123Z",
                           "updated_at": "2026-09-01T10:00:00Z"}])

    def test_imported_memory_updated_then_forgotten_takes_first_seen_from_the_ledger(self):
        root, _ = self.init({}, [
            self.event(1, "memory_set", "k", "2026-10-01T00:00:00Z", old_hash="h0", new_hash="h1"),
            self.event(2, "memory_deleted", "k", "2026-10-02T00:00:00Z", old_hash="h1",
                       old_value="b")], ledger={"k": {"first_seen": "2026-01-01"}})
        self.assertEqual(rows(root, "SELECT first_seen, deleted_at FROM memory_tombstones"),
                         [{"first_seen": "2026-01-01", "deleted_at": "2026-10-02T00:00:00Z"}])

    def test_two_deletes_keep_the_first_life_start(self):
        root, _ = self.init({}, [
            self.event(1, "memory_set", "k", "2026-10-01T00:00:00Z", old_hash=None, new_hash="h1"),
            self.event(2, "memory_deleted", "k", "2026-10-02T00:00:00Z", old_hash="h1"),
            self.event(3, "memory_deleted", "k", "2026-10-03T00:00:00Z", old_hash="h1")])
        self.assertEqual(rows(root, "SELECT first_seen, deleted_at FROM memory_tombstones"),
                         [{"first_seen": "2026-10-01T00:00:00Z", "deleted_at": "2026-10-03T00:00:00Z"}])

    def test_a_live_key_with_a_trailing_delete_keeps_its_times(self):
        root, said = self.init({"k": "v"}, [
            self.event(1, "memory_set", "k", "2026-10-01T00:00:00Z", old_hash=None, new_hash="h1"),
            self.event(2, "memory_set", "k", "2026-10-05T00:00:00Z", old_hash="h1", new_hash="h2"),
            self.event(3, "memory_deleted", "k", "2026-10-06T00:00:00Z", old_hash="h2")])
        self.assertIn("is in the export and its later history event is memory_deleted", said)
        self.assertEqual(rows(root, "SELECT created_at, updated_at FROM memories"),
                         [{"created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-05T00:00:00Z"}])
        self.assertEqual(rows(root, "SELECT key FROM memory_tombstones"), [])

    def test_a_same_body_first_event_leaves_the_ledger_date(self):
        root, _ = self.init({"k": "same"}, [
            self.event(1, "memory_set", "k", "2026-10-01T00:00:00Z", old_hash="h", new_hash="h")],
            ledger={"k": {"first_seen": "2026-01-01", "last_changed": "2026-06-01"}})
        self.assertEqual(rows(root, "SELECT created_at, updated_at FROM memories"),
                         [{"created_at": "2026-01-01", "updated_at": "2026-06-01"}])

    def test_a_body_change_event_wins_over_a_later_ledger_date(self):
        # The ledger's last_changed is the day bd-memgraph looked, not the day the body changed.
        root, _ = self.init({"k": "v2"}, [
            self.event(1, "memory_set", "k", "2026-09-07T15:36:22Z", old_hash="h1", new_hash="h2")],
            ledger={"k": {"first_seen": "2026-01-01", "last_changed": "2026-09-30"}})
        self.assertEqual(rows(root, "SELECT updated_at FROM memories"),
                         [{"updated_at": "2026-09-07T15:36:22Z"}])

    def test_a_set_for_a_key_not_in_the_export_warns(self):
        _, said = self.init({}, [
            self.event(1, "memory_set", "k", "2026-10-01T00:00:00Z", old_hash=None, new_hash="h1")])
        self.assertIn("memory k has a memory_set event and is not in the export; no tombstone", said)


class RankTest(WritableCase):
    def setUp(self) -> None:
        super().setUp()
        self.w("remember", "--key", "hub", "the centre, 2026-11-01")
        for name in ("a1", "a2", "a3"):
            self.w("remember", "--key", name, f"points at [[hub]], 2026-11-01 ({name})")
        self.w("remember", "--key", "old-way", "the old way [[hub]] 2026-11-01")
        self.w("remember", "--key", "new-way", "supersedes::[[old-way]] [[hub]] 2026-11-01")

    def ranked(self, *argv: str):
        code, out, err = self.cli("memories", "--rank", "--today", "2026-11-08", "--json", *argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_the_hub_ranks_first_and_superseded_takes_a_tenth(self):
        ranked = self.ranked()
        self.assertEqual(ranked[0]["key"], "hub")
        self.assertEqual(ranked[0]["degree"], 5)
        old = next(r for r in ranked if r["key"] == "old-way")
        new = next(r for r in ranked if r["key"] == "new-way")
        self.assertTrue(old["superseded"])
        self.assertLess(old["score"], new["score"] / 5)
        self.assertEqual(ranked[-1]["key"], "old-way")

    def test_recency(self):
        conn = sqlite3.connect(db_path(self.store_dir))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        today = memgraph.datetime.date(2026, 11, 8)
        self.w("remember", "--key", "undated", "no date here")
        conn.execute("UPDATE memories SET updated_at = '2026-11-08T00:00:00Z'")   # not the clock
        by_key = {r["key"]: r for r in memgraph.rank(conn, today)}
        self.assertEqual(by_key["hub"]["age_days"], 0)        # updated today (the write)
        self.assertEqual(by_key["hub"]["recency"], 1.0)
        far = {r["key"]: r for r in memgraph.rank(conn, memgraph.datetime.date(2027, 2, 6))}
        self.assertAlmostEqual(far["hub"]["recency"], 0.5, places=2)     # ~90 days on
        conn.execute("UPDATE memories SET updated_at = NULL WHERE key = 'undated'")
        self.assertEqual({r["key"]: r for r in memgraph.rank(conn, today)}["undated"]["recency"],
                         memgraph.UNDATED_RECENCY)

    def test_superseded_takes_exactly_a_tenth(self):
        with_factor = {r["key"]: r["score"] for r in self.ranked()}
        with mock.patch.object(memgraph, "SUPERSEDED_FACTOR", 1.0):
            without = {r["key"]: r["score"] for r in self.ranked()}
        self.assertAlmostEqual(with_factor["old-way"], without["old-way"] * 0.1)
        self.assertEqual(with_factor["new-way"], without["new-way"])

    def test_ties_go_by_recency_then_key(self):
        days = (("z-new", "2026-11-01"), ("m-old", "2026-01-01"), ("a-old", "2026-01-01"))
        for key, _ in days:
            self.w("remember", "--key", key, f"an orphan {key}")
        conn = sqlite3.connect(db_path(self.store_dir))
        self.addCleanup(conn.close)
        for key, day in days:
            conn.execute("UPDATE memories SET updated_at = ? WHERE key = ?", (day + "T00:00:00Z", key))
        conn.commit()
        conn.row_factory = sqlite3.Row
        today = memgraph.datetime.date(2026, 11, 8)
        # Recency weight 0: every orphan scores the same; recency then key break the tie.
        ranked = [r["key"] for r in memgraph.rank(conn, today, recency_weight=0)
                  if r["key"] in ("z-new", "m-old", "a-old")]
        self.assertEqual(ranked, ["z-new", "a-old", "m-old"])

    def test_a_keyword_does_not_change_the_scores(self):
        whole = {r["key"]: r["score"] for r in self.ranked()}
        self.assertEqual([(r["key"], r["score"]) for r in self.ranked("a2")], [("a2", whole["a2"])])

    def test_keyword_limit_and_text(self):
        self.assertEqual([r["key"] for r in self.ranked("a2")], ["a2"])
        self.assertEqual(len(self.ranked("-n", "2")), 2)
        code, out, _ = self.cli("memories", "--rank", "--today", "2026-11-08", "-n", "1")
        self.assertEqual(out.splitlines()[0].split(), ["score", "deg", "age", "key"])
        self.assertTrue(out.splitlines()[1].endswith("  hub"))
        code, _, err = self.cli("memories", "--rank", "--today", "nope")
        self.assertEqual(code, 2)
        self.assertEqual(self.cli("memories", "--rank", "--recency-weight", "2")[0], 2)

    def test_plain_memories_is_unchanged(self):
        code, out, _ = self.cli("memories")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Memories (6):"))


class RelatedTest(WritableCase):
    def setUp(self) -> None:
        super().setUp()
        self.issue = self.new("an issue")
        self.w("remember", "--key", "base", "the base fact")
        self.w("remember", "--key", "mid", f"depends-on::[[base]] see [[{self.issue}]]")
        self.w("remember", "--key", "top", "refines::[[mid]] and [[nowhere-key]]")

    def related(self, *argv: str):
        code, out, err = self.cli("related", *argv, "--json")
        self.assertEqual(code, 0, err)
        return [(i["direction"], i["level"], i["relation"], i["key"], i["kind"], i["exists"])
                for i in json.loads(out)]

    def test_one_hop(self):
        self.assertEqual(self.related("mid"), [
            ("out", 1, "depends-on", "base", "memory", True),
            ("out", 1, "related", self.issue, "issue", True),
            ("in", 1, "refines", "top", "memory", True)])

    def test_depth_walks_like_lineage(self):
        self.assertEqual(self.related("base", "--depth", "2"), [
            ("in", 1, "depends-on", "mid", "memory", True),
            ("in", 2, "refines", "top", "memory", True)])
        out = self.related("top", "--depth", "3")
        self.assertIn(("out", 2, "depends-on", "base", "memory", True), out)
        self.assertIn(("out", 1, "related", "nowhere-key", "memory", False), out)

    def test_text_and_errors(self):
        code, out, _ = self.cli("related", "top")
        self.assertEqual(code, 0)
        self.assertIn("    --related--> nowhere-key (no such memory)", out)
        self.assertIn("    --refines--> mid: depends-on::[[base]]", out)
        self.assertIn("in (linked from):\n    (none)", out)
        code, _, err = self.cli("related", "never")
        self.assertEqual((code, err.strip()), (1, 'no memory with key "never"'))
        self.assertEqual(self.cli("related", "top", "--depth", "0")[0], 2)
