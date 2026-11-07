"""`cairn history` (gap-closure spec, section 3): the events of one issue or one memory key, in
recording order, as `export --events` objects or as the text the spec fixes. Real stores made by
`cairn init`, written through the CLI."""

from __future__ import annotations

import json
import sqlite3

from cairn.queries import event_objects
from cairn.store import db_path, open_db
from tests.helpers import StoreCase, issue, memory
from tests.test_writer import WritableCase


class HistoryTest(WritableCase):
    def setUp(self) -> None:
        super().setUp()
        self.id = self.new("the title")
        self.w("update", self.id, "--priority", "1")
        self.w("comment", self.id, "a comment")
        self.w("close", self.id)

    def history(self, *argv: str):
        return self.cli("history", *argv)

    def json_history(self, *argv: str):
        code, out, err = self.history(*argv, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_order_is_recording_order(self):
        steps = [(e["kind"], e["data"].get("field")) for e in self.json_history(self.id)]
        self.assertEqual(steps[0], ("created", None))
        comment = steps.index(("comment_added", None))
        self.assertLess(steps.index(("field_change", "priority")), comment)
        self.assertGreater(steps.index(("field_change", "status")), comment)   # the close

    def test_json_is_the_export_object(self):
        conn = open_db(db_path(self.store_dir))
        self.addCleanup(conn.close)
        exported = [e for e in event_objects(conn) if e["issue_id"] == self.id]
        self.assertEqual(self.json_history(self.id), exported)

    def test_limit_keeps_the_newest_tail_oldest_first(self):
        every = self.json_history(self.id)
        self.assertEqual(self.json_history(self.id, "--limit", "1"), every[-1:])
        self.assertEqual(self.json_history(self.id, "--limit", "2"), every[-2:])
        self.assertEqual(self.json_history(self.id, "--limit", "0"), every)
        self.assertEqual(self.json_history(self.id, "--limit", "99"), every)
        code, _, _ = self.history(self.id, "--limit", "-1")
        self.assertEqual(code, 2)

    def test_kind_filters_and_an_unknown_kind_is_empty(self):
        changes = self.json_history(self.id, "--kind", "field_change")
        self.assertTrue(changes)
        self.assertEqual({e["kind"] for e in changes}, {"field_change"})
        self.assertEqual(self.json_history(self.id, "--kind", "no_such_kind"), [])

    def test_unknown_issue(self):
        code, out, err = self.history("t-999", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), 'Error fetching t-999: no issue found matching "t-999"')

    def test_issue_without_events(self):
        bare = self.new("bare")
        # An issue imported from bd has a row and no events; make one by hand. The events
        # table is append-only by trigger, so drop the trigger on this scratch store.
        writable = sqlite3.connect(db_path(self.store_dir))
        triggers = [r[0] for r in writable.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'events'")]
        for name in triggers:
            writable.execute(f'DROP TRIGGER "{name}"')
        writable.execute("DELETE FROM events WHERE issue_id = ?", (bare,))
        writable.commit()
        writable.close()
        self.assertEqual(self.json_history(bare), [])
        code, out, _ = self.history(bare)
        self.assertEqual((code, out.strip()), (0, f"No events for {bare}"))

    def test_text_follows_the_sample(self):
        code, out, err = self.history(self.id, "--limit", "0")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        events = self.json_history(self.id)
        self.assertEqual(lines[0], f"History for {self.id} ({len(events)} events)")
        self.assertEqual(lines[1], "")
        created = events[0]
        self.assertEqual(lines[2], f"{created['created_at']}  created  actor=alice")
        self.assertEqual(lines[3:5], ["  parent: null", "  title: the title"])
        change = lines.index(f"{events[1]['created_at']}  field_change  actor=alice", 5)
        self.assertEqual(lines[change + 1:change + 4],
                         ["  field: priority", "  new_value: 1", "  old_value: 2"])

    def test_deleted_issue_keeps_its_history(self):
        self.w("delete", self.id, "--force")
        code, _, _ = self.cli("show", self.id)
        self.assertEqual(code, 1)
        events = self.json_history(self.id)
        deleted = [e for e in events if e["kind"] == "deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["data"]["issue"]["id"], self.id)
        code, out, _ = self.history(self.id, "--kind", "deleted")
        payload = [line for line in out.splitlines() if line.startswith("  issue: ")]
        self.assertEqual(len(payload), 1)
        nested = json.loads(payload[0][len("  issue: "):])
        self.assertEqual(payload[0][len("  issue: "):], json.dumps(nested, sort_keys=True,
                                                                   ensure_ascii=False))

    def test_memory_history_survives_forget(self):
        self.w("remember", "--key", "k", "first body")
        self.w("remember", "--key", "k", "second\nbody")
        self.w("forget", "k")
        code, _, _ = self.cli("recall", "k")
        self.assertEqual(code, 1)
        events = self.json_history("--memory", "k")
        self.assertEqual([e["kind"] for e in events], ["memory_set", "memory_set", "memory_deleted"])
        self.assertEqual(events[1]["data"]["old_value"], "first body")
        self.assertEqual(events[2]["data"]["old_value"], "second\nbody")
        code, out, _ = self.history("--memory", "k", "--kind", "memory_deleted")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("History for memory k (1 event)\n"))
        self.assertIn("  old_value: second\n    body\n", out)    # continuation lines indented

    def test_unknown_memory(self):
        code, out, err = self.history("--memory", "never", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.strip(), 'no memory with key "never"')

    def test_id_and_memory_together_or_neither(self):
        self.assertEqual(self.history(self.id, "--memory", "k")[0], 2)
        self.assertEqual(self.history()[0], 2)

    def test_reading_writes_nothing(self):
        before = self.cli("export", "--events")[1]
        self.history(self.id)
        self.history("--memory", "k")
        self.assertEqual(self.cli("export", "--events")[1], before)


class ShadowHistoryTest(StoreCase):
    def test_answers_on_a_shadow(self):
        self.build([issue("bd-1"), memory("m", "body")])
        code, out, err = self.cli("history", "bd-1")
        self.assertEqual((code, out.strip()), (0, "No events for bd-1"), err)
        code, out, err = self.cli("history", "--memory", "m", "--json")
        self.assertEqual((code, json.loads(out)), (0, []), err)
