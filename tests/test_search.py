"""`cairn search` and `cairn check --stale` (workspace-d3ct)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess

from cairn import search as searchmod
from cairn.store import db_path

from .helpers import StoreCase, issue, memory


class SearchTest(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn, _ = self.build([
            issue("ws-a1", title="Nightly consolidation job fails on timeout",
                  description="the daemon is unreachable at 04:30"),
            issue("ws-b2", title="Unrelated landing page copy", notes="mentions consolidation once"),
            issue("ws-c3", title="Old consolidation bug", status="closed",
                  closed_at="2026-09-20T00:00:00Z"),
            memory("consolidation-daemon-gotchas", "the 04:30 job needs the daemon up; check the unit"),
            memory("landing-deploy", "deploy through the static host"),
        ])

    def test_ranks_a_title_match_above_a_notes_mention(self):
        ids = [r["id"] for r in searchmod.search(self.conn, "consolidation timeout", kinds=("issue",))]
        self.assertEqual(ids[0], "ws-a1")
        self.assertLess(ids.index("ws-c3"), ids.index("ws-b2"))

    def test_kinds_and_closed(self):
        rows = searchmod.search(self.conn, "consolidation daemon")
        self.assertEqual({r["kind"] for r in rows}, {"issue", "memory"})
        mem = searchmod.search(self.conn, "consolidation daemon", kinds=("memory",))
        self.assertEqual(mem[0]["id"], "consolidation-daemon-gotchas")
        self.assertIsNone(mem[0]["status"])
        open_only = searchmod.search(self.conn, "consolidation", kinds=("issue",), include_closed=False)
        self.assertNotIn("ws-c3", [r["id"] for r in open_only])

    def test_any_word_may_match_and_stopwords_alone_find_nothing(self):
        self.assertEqual([r["id"] for r in searchmod.search(self.conn, "deploy zzzz")], ["landing-deploy"])
        self.assertEqual(searchmod.search(self.conn, "the of and"), [])

    def test_quotes_and_fts_syntax_in_the_query_are_plain_words(self):
        self.assertEqual(searchmod.search(self.conn, 'NEAR("x" "y") OR * :'), [])
        self.assertTrue(searchmod.search(self.conn, 'consolidation"; DROP'))

    def test_cli_text_json_and_refusals(self):
        code, out, _ = self.cli("search", "consolidation", "timeout", "--issues", "-n", "1")
        self.assertEqual(code, 0)
        self.assertIn("ws-a1  [open]  Nightly consolidation job fails on timeout", out)
        self.assertIn("[consolidation]", out)
        code, out, _ = self.cli("search", "--json", "deploy", "--memories")
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in json.loads(out)], ["landing-deploy"])
        self.assertEqual(self.cli("search", "the", "of")[0], 2)
        self.assertEqual(self.cli("search", "x", "--issues", "--memories")[0], 2)
        code, out, _ = self.cli("search", "nothingmatchesthis")
        self.assertEqual((code, out.strip()), (0, "(no matches)"))


class StaleTest(StoreCase):
    def git(self, *args: str, when: str = "2026-10-01T12:00:00Z") -> None:
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when,
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull)
        subprocess.run(["git", "-C", str(self.workspace), *args], check=True, env=env,
                       capture_output=True)

    def setUp(self) -> None:
        super().setUp()
        self.conn, _ = self.build([
            issue("ws-old", status="closed", closed_at="2026-10-10T00:00:00Z"),
            issue("ws-same", status="closed", closed_at="2026-10-01T13:00:00Z"),
            issue("ws-open"),
            memory("pending-work", "waits for ws-old and [[ws-open]]; also ws-same; not ws-oldish"),
            memory("tool-notes", "run tools/guard.sh first, and tools/kept.sh; see other/never.md"),
            memory("undated", "names ws-old"),
        ])
        rw = sqlite3.connect(db_path(self.store_dir))
        rw.execute("UPDATE memories SET updated_at = '2026-10-01T12:00:00Z' WHERE key != 'undated'")
        rw.commit()
        rw.close()
        self.git("init", "-q")
        (self.workspace / "tools").mkdir()
        for name in ("guard.sh", "kept.sh"):
            (self.workspace / "tools" / name).write_text("x\n")
        self.git("add", "tools")
        self.git("commit", "-q", "-m", "add", when="2026-09-01T00:00:00Z")
        self.git("rm", "-q", "tools/guard.sh")
        self.git("commit", "-q", "-m", "rm", when="2026-10-05T00:00:00Z")

    def test_issue_closed_after_the_memory_and_path_deleted_after_it(self):
        code, out, _ = self.cli("check", "--stale")
        self.assertEqual(code, 0)
        stale = [line for line in out.splitlines() if line.startswith("⌛")]
        self.assertEqual(stale, [
            ("⌛ memory pending-work names ws-old, closed 2026-10-10, after the memory last "
             "changed (2026-10-01)"),
            ("⌛ memory tool-notes names tools/guard.sh, deleted 2026-10-05, after the memory "
             "last changed (2026-10-01)"),
        ])                                 # ws-same: within the grace; ws-open: open
        self.assertIn("1 memories have no recorded write time", out)
        self.assertIn("2 stale anchors", out)

    def test_plain_check_is_unchanged_and_json_carries_the_findings(self):
        code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertNotIn("stale", out)
        code, out, _ = self.cli("check", "--stale", "--json")
        found = json.loads(out)["stale"]
        self.assertEqual([(f["key"], f["anchor"], f["target"]) for f in found],
                         [("pending-work", "issue", "ws-old"), ("tool-notes", "path", "tools/guard.sh")])

    def test_without_git_only_paths_go_unchecked(self):
        subprocess.run(["rm", "-rf", str(self.workspace / ".git")], check=True)
        code, out, _ = self.cli("check", "--stale")
        self.assertEqual(code, 0)
        self.assertIn("names ws-old", out)
        self.assertIn("paths not checked", out)
