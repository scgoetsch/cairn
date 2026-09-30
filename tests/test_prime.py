from __future__ import annotations

from cairn import prime

from .helpers import NOW, StoreCase, issue, memory


class PrimeTest(StoreCase):
    def build_with_hot(self, rows, hot_keys):
        hot = self.workspace / "hot.txt"
        hot.write_text("".join(k + "\n" for k in hot_keys), encoding="utf-8")
        conn, _ = self.build(rows, hot_path=hot)
        return conn

    def index_keys(self, text: str):
        tail = text.split("## Persistent Memories — index", 1)[1]
        return [line[2:] for line in tail.splitlines() if line.startswith("- ")]

    def test_hot_memories_in_full_and_the_rest_by_key(self):
        conn = self.build_with_hot(
            [memory("guard", "NEVER do the thing."), memory("cold-a", "SECRET-A body"),
             memory("cold-b", "SECRET-B body")], ["guard"])
        text = prime.build(conn, now=NOW)
        self.assertIn("HOT tier (1 in full of 3 total)", text)
        self.assertIn("### guard\nNEVER do the thing.", text)
        self.assertEqual(self.index_keys(text), ["cold-a", "cold-b"])
        self.assertNotIn("SECRET-A", text)

    def test_empty_hot_list_is_valid_and_indexes_everything(self):
        conn = self.build_with_hot([memory("a", "x"), memory("b", "y")], [])
        text = prime.build(conn, now=NOW)
        self.assertIn("HOT tier (0 in full of 2 total)", text)
        self.assertEqual(self.index_keys(text), ["a", "b"])
        self.assertNotIn("⚠", text)

    def test_over_budget_hot_bodies_move_to_the_index_and_the_payload_says_so(self):
        conn = self.build_with_hot(
            [memory("big", "B" * 5000), memory("small", "tiny guard"), memory("cold", "c")],
            ["big", "small"])
        text = prime.build(conn, now=NOW, budget=2000)
        self.assertLessEqual(len(text.encode("utf-8")), 2000)
        self.assertIn("### small\ntiny guard", text)
        self.assertNotIn("B" * 100, text)
        self.assertIn("1 hot memory(ies) did not fit the 2000-byte budget", text)
        self.assertIn("instead: big", text)
        self.assertEqual(self.index_keys(text), ["big", "cold"])

    def test_every_memory_is_listed_exactly_once(self):
        keys = [f"m-{i:02d}" for i in range(30)]
        conn = self.build_with_hot([memory(k, "body " * 50) for k in keys], keys[:10])
        text = prime.build(conn, now=NOW, budget=1500)
        in_full = [line[4:] for line in text.splitlines() if line.startswith("### ")]
        self.assertEqual(sorted(in_full + self.index_keys(text)), keys)

    def test_a_budget_too_small_for_the_index_is_flagged_at_the_top(self):
        conn = self.build_with_hot([memory(f"key-{i:03d}", "b") for i in range(60)], [])
        text = prime.build(conn, now=NOW, budget=300)
        self.assertTrue(text.startswith("> ⚠ Payload is "))
        self.assertEqual(len(self.index_keys(text)), 60)

    def test_work_section(self):
        conn = self.build_with_hot([
            issue("t-a", priority=0), issue("t-b", status="in_progress"),
            issue("t-due", status="deferred", defer_until="2026-09-01T00:00:00Z"),
            issue("t-later", status="deferred", defer_until="2026-12-01T00:00:00Z"),
        ], [])
        text = prime.build(conn, now=NOW)
        self.assertIn("- In progress: 1 — t-b", text)
        self.assertIn("- Ready: 1", text)
        self.assertIn("  - t-a P0 title of t-a", text)
        self.assertIn("Deferred and now due (1)", text)
        self.assertIn("t-due", text)
        self.assertNotIn("t-later", text)
