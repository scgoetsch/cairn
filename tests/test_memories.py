from __future__ import annotations

import json

from cairn import links
from cairn.importer import body_hash

from .helpers import StoreCase, issue, memory


class ParseTest(StoreCase):
    def test_typed_and_untyped_links(self):
        edges, unknown = links.parse_edges({
            "new": "supersedes::[[old]] and [[other]] and depends-on::[[other]]",
            "old": "plain body",
            "other": "explicit related::[[old]]",
        })
        self.assertEqual(unknown, [])
        self.assertEqual(sorted(edges), [
            ("new", "depends-on", "other", "memory"),
            ("new", "related", "other", "memory"),
            ("new", "supersedes", "old", "memory"),
            ("other", "related", "old", "memory"),
        ])

    def test_issue_or_memory_is_decided_by_what_exists_then_by_shape(self):
        edges, _ = links.parse_edges({
            "src": "[[proj-7m8.2]] [[proj-gone]] [[proj-notes]] [[other-abc]] [[WS-abc]]",
            "proj-notes": "a memory whose key happens to look like an issue id",
        }, issue_ids={"proj-7m8.2"}, prefixes=["proj"])
        self.assertEqual({target: kind for _, _, target, kind in edges}, {
            "proj-7m8.2": "issue",      # an issue in the store
            "proj-gone": "issue",       # not in the store, but shaped like one of its ids
            "proj-notes": "memory",     # an existing memory wins over the shape
            "other-abc": "memory",      # another prefix: nothing says it is an issue
            "WS-abc": "memory",
        })

    def test_extra_issue_prefixes_are_case_insensitive(self):
        edges, _ = links.parse_edges({"src": "[[WS-abc]] [[ws-def.3]]"},
                                     prefixes=["proj", "ws"])
        self.assertEqual({kind for _, _, _, kind in edges}, {"issue"})

    def test_notation_examples_are_not_links(self):
        edges, _ = links.parse_edges(
            {"doc": "write [[k]] or supersedes::[[k]]; [[key]] and [[wikilinks]] are prose"})
        self.assertEqual(edges, [])

    def test_unknown_relation_is_kept_as_related_and_reported(self):
        edges, unknown = links.parse_edges({"src": "frobs::[[dst]]", "dst": ""})
        self.assertEqual(edges, [("src", "related", "dst", "memory")])
        self.assertEqual(unknown, [("src", "frobs", "dst")])

    def test_duplicate_links_collapse(self):
        edges, _ = links.parse_edges({"src": "[[dst]] again [[dst]]", "dst": ""})
        self.assertEqual(len(edges), 1)

    def test_a_key_that_only_looks_like_another_prefix_is_a_memory(self):
        edges, _ = links.parse_edges({"src": "[[other-abc]]"}, prefixes=["proj"])
        self.assertEqual(edges, [("src", "related", "other-abc", "memory")])

    def test_without_any_prefix_nothing_is_guessed_to_be_an_issue(self):
        edges, _ = links.parse_edges({"src": "[[workspace-abc]] [[ws-1]]"})
        self.assertEqual({kind for _, _, _, kind in edges}, {"memory"})


class StoreMemoryTest(StoreCase):
    def test_hash_timestamps_and_hot_flag(self):
        ledger = self.workspace / "ledger.json"
        ledger.write_text(json.dumps({
            "k-old": {"first_seen": "2026-08-09", "last_changed": "2026-09-01", "hash": "x"},
            "k-new": {"first_seen": "2026-09-20", "last_changed": None, "hash": "y"},
            "k-gone": {"first_seen": "2026-08-09", "deleted_at": "2026-09-30", "hash": "z"},
        }), encoding="utf-8")
        hot = self.workspace / "hot.txt"
        hot.write_text("k-old\n\nk-missing\nk-old\n", encoding="utf-8")
        conn, report = self.build(
            [memory("k-old", "old body"), memory("k-new", "new body"), memory("k-bare", "b")],
            ledger_path=ledger, hot_path=hot)
        rows = {r["key"]: r for r in conn.execute("SELECT * FROM memories")}
        self.assertEqual(rows["k-old"]["body_hash"], body_hash("old body"))
        self.assertEqual((rows["k-old"]["created_at"], rows["k-old"]["updated_at"]),
                         ("2026-08-09", "2026-09-01"))
        self.assertEqual((rows["k-new"]["created_at"], rows["k-new"]["updated_at"]),
                         ("2026-09-20", "2026-09-20"))
        self.assertIsNone(rows["k-bare"]["created_at"])
        self.assertEqual({k: r["hot"] for k, r in rows.items()},
                         {"k-old": 1, "k-new": 0, "k-bare": 0})
        self.assertEqual(report.warnings,
                         ["hot list names a memory that does not exist: k-missing"])
        self.assertEqual(report.counts["memory_tombstones"], 1)

    def test_check_flags_dangling_and_self_links_and_explains_deleted_targets(self):
        ledger = self.workspace / "ledger.json"
        ledger.write_text(json.dumps(
            {"k-gone": {"first_seen": "2026-08-09", "deleted_at": "2026-09-30"}}),
            encoding="utf-8")
        self.build([
            memory("first", "[[k-gone]] and [[never-existed]] and [[first]]"),
            memory("second", "supersedes::[[first]] refers to [[t-1]] and [[t-404]]"),
            issue("t-1"),
        ], ledger_path=ledger)
        code, out, _ = self.cli("check", "--json")
        result = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(result["errors"]), [
            "dangling link: memory first -related-> k-gone (deleted 2026-09-30)",
            "dangling link: memory first -related-> never-existed",
            "self link: memory first",
        ])
        self.assertEqual(result["warnings"],
                         ["unswept supersedes: second supersedes first, which is still stored"])
        self.assertEqual(result["notes"],
                         ["memory second refers to issue t-404, which is not in this store"])

    def test_extra_prefixes_come_from_the_environment(self):
        rows = [memory("src", "see [[WS-abc]] and [[t-404]]"), issue("t-1")]
        conn, _ = self.build(rows, env={})
        kinds = {r["target"]: r["target_kind"] for r in conn.execute("SELECT * FROM memory_edges")}
        self.assertEqual(kinds, {"WS-abc": "memory", "t-404": "issue"})
        conn, _ = self.build(rows, env={"CAIRN_ISSUE_PREFIXES": "zz, ws"})
        kinds = {r["target"]: r["target_kind"] for r in conn.execute("SELECT * FROM memory_edges")}
        self.assertEqual(kinds, {"WS-abc": "issue", "t-404": "issue"})

    def test_a_memory_keyed_like_the_reserved_json_key_is_flagged(self):
        self.build([memory("schema_version", "an unlucky key"), memory("other", "fine")])
        code, out, _ = self.cli("check", "--json")
        self.assertEqual(code, 0)
        self.assertIn("schema_version", json.loads(out)["warnings"][0])

    def test_check_passes_on_a_clean_graph(self):
        self.build([memory("one", "refines::[[two]]"), memory("two", "see [[t-1]]"),
                    issue("t-1")])
        code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertIn("✓ OK", out)


class MemoryVerbTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.build([memory("harbour-notes", "The LIGHTHOUSE keeper logs tide 42."),
                    memory("other", "nothing relevant " + "x" * 200)])

    def test_recall_prints_the_body(self):
        code, out, _ = self.cli("recall", "harbour-notes")
        self.assertEqual((code, out), (0, "The LIGHTHOUSE keeper logs tide 42.\n"))

    def test_recall_of_a_missing_key_fails_like_bd(self):
        code, out, _ = self.cli("recall", "no-such-key")
        self.assertEqual((code, out), (1, 'No memory with key "no-such-key"\n'))
        code, out, _ = self.cli("recall", "no-such-key", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"found": False, "key": "no-such-key",
                                           "schema_version": 1})

    def test_search_is_case_insensitive_over_key_and_body(self):
        code, out, _ = self.cli("memories", "lighthouse", "--json")
        self.assertEqual(json.loads(out), {
            "harbour-notes": "The LIGHTHOUSE keeper logs tide 42.", "schema_version": 1})
        _, out, _ = self.cli("memories", "TIDE 42", "--json")
        self.assertEqual(set(json.loads(out)), {"harbour-notes", "schema_version"})
        _, out, _ = self.cli("memories", "HARBOUR", "--json")       # matches the key
        self.assertEqual(set(json.loads(out)), {"harbour-notes", "schema_version"})
        _, out, _ = self.cli("memories", "no such text", "--json")
        self.assertEqual(json.loads(out), {"schema_version": 1})

    def test_listing_previews_long_bodies(self):
        _, out, _ = self.cli("memories")
        self.assertIn("Memories (2):", out)
        self.assertIn("...", out)
        self.assertNotIn("x" * 150, out)
