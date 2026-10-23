from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cairn import cli as cli_module
from cairn.store import STORE_DIRNAME, find_store_dir

from .helpers import StoreCase, child, dep, issue, with_deps

ROWS = [
    issue("t-epic", issue_type="epic", priority=1),
    child("t-epic.1", "t-epic", status="in_progress", assignee="ann",
          started_at="2026-09-11T10:00:00Z"),
    with_deps(issue("t-blocked", issue_type="bug"), dep("t-blocked", "t-epic.1")),
    issue("t-done", status="closed", closed_at="2026-09-12T10:00:00Z", close_reason="ok"),
    issue("t-parked", status="deferred", defer_until="2026-01-01T00:00:00Z"),
]


class HookContractTest(StoreCase):
    """The shapes the existing session hooks parse (bd-prerun-hook.sh,
    bd-stop-hook.sh): a JSON array, or text rows that start with the ◐ glyph."""

    def setUp(self):
        super().setUp()
        self.build(ROWS)

    def test_json_flag_works_before_and_after_the_verb(self):
        before = self.cli("--json", "list", "--status=in_progress")
        after = self.cli("list", "--status=in_progress", "--json")
        self.assertEqual(before, after)
        data = json.loads(before[1])
        self.assertIsInstance(data, list)
        self.assertEqual([i["id"] for i in data], ["t-epic.1"])
        self.assertEqual(data[0]["status"], "in_progress")
        self.assertEqual(data[0]["parent"], "t-epic")

    def test_empty_result_is_an_empty_json_array(self):
        code, out, _ = self.cli("list", "--status=blocked", "--json")
        self.assertEqual((code, json.loads(out)), (0, []))

    def test_text_rows_start_with_the_status_glyph(self):
        _, out, _ = self.cli("list", "--status=in_progress")
        rows = [line for line in out.splitlines() if line.startswith("◐ ")]
        self.assertEqual(rows, ["◐ t-epic.1 ● P2 title of t-epic.1"])
        # the legend mentions every glyph but must not look like an issue row
        self.assertFalse(any(line.startswith("◐ ") for line in out.splitlines()[1:]))

    def test_list_hides_closed_unless_asked(self):
        ids = lambda *argv: [i["id"] for i in json.loads(self.cli(*argv, "--json")[1])]
        self.assertNotIn("t-done", ids("list"))
        self.assertIn("t-done", ids("list", "--all"))
        self.assertEqual(ids("list", "--status=closed"), ["t-done"])
        self.assertEqual(set(ids("list", "--status=open,deferred")),
                         {"t-epic", "t-blocked", "t-parked"})

    def test_limit(self):
        ids = lambda *argv: [i["id"] for i in json.loads(self.cli(*argv, "--json")[1])]
        self.assertEqual(len(ids("list", "--all", "--limit", "2")), 2)
        self.assertEqual(len(ids("list", "--all", "--limit", "0")), 5)

    def test_default_limits_match_bd_and_a_truncated_json_listing_says_so(self):
        self.build([issue(f"t-{n:03d}") for n in range(120)])
        for verb, default in (("list", 50), ("ready", 100)):
            code, out, err = self.cli(verb, "--json")
            self.assertEqual((code, len(json.loads(out))), (0, default), verb)
            self.assertEqual(
                err, f"cairn: showing {default} of 120 issues (use --limit 0 for all)\n")
            code, out, err = self.cli(verb, "--json", "--limit", "0")
            self.assertEqual((len(json.loads(out)), err), (120, ""), verb)
            _, text, err = self.cli(verb)
            self.assertEqual(err, "")
            self.assertIn("120", text)
        _, out, err = self.cli("list", "--json", "--limit", "120")
        self.assertEqual((len(json.loads(out)), err), (120, ""))

    def test_ready_and_blocked(self):
        _, out, _ = self.cli("ready", "--json")
        self.assertEqual([i["id"] for i in json.loads(out)], ["t-epic"])
        _, out, _ = self.cli("blocked", "--json")
        blocked = json.loads(out)
        self.assertEqual([(i["id"], i["blocked_by"], i["blocked_by_count"]) for i in blocked],
                         [("t-blocked", ["t-epic.1"], 1)])
        _, text, _ = self.cli("ready")
        self.assertIn("○ t-epic ● P1 [epic] title of t-epic", text)
        self.assertIn("Ready: 1 issues with no active blockers", text)

    def test_show(self):
        code, out, _ = self.cli("show", "t-blocked", "--json")
        obj = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(obj), 1)
        self.assertEqual(obj[0]["dependencies"][0]["id"], "t-epic.1")
        self.assertEqual(obj[0]["dependencies"][0]["dependency_type"], "blocks")
        code, out, _ = self.cli("show", "t-epic.1", "--json")
        self.assertEqual(json.loads(out)[0]["dependents"][0]["id"], "t-blocked")
        _, text, _ = self.cli("show", "t-blocked")
        self.assertIn("BLOCKED BY\n  t-epic.1", text)

    def test_show_counts_every_edge_type_but_list_counts_only_blocks(self):
        # bd is inconsistent here and cairn reproduces it: an epic's children
        # count as dependents in `show`, not in `list`.
        shown = json.loads(self.cli("show", "t-epic", "--json")[1])[0]
        listed = {i["id"]: i for i in json.loads(self.cli("list", "--json")[1])}
        self.assertEqual((shown["dependent_count"], listed["t-epic"]["dependent_count"]), (1, 0))
        child_shown = json.loads(self.cli("show", "t-epic.1", "--json")[1])[0]
        self.assertEqual((child_shown["dependency_count"], child_shown["dependent_count"]),
                         (1, 1))
        nested = child_shown["dependencies"][0]
        self.assertEqual((nested["id"], nested["dependency_type"]), ("t-epic", "parent-child"))
        self.assertNotIn("dependency_count", nested)
        self.assertEqual(nested["title"], "title of t-epic")

    def test_blocked_objects_omit_counts_and_edges(self):
        blocked = json.loads(self.cli("blocked", "--json")[1])[0]
        for key in ("dependencies", "parent", "dependency_count", "dependent_count",
                    "comment_count"):
            self.assertNotIn(key, blocked)
        self.assertEqual(blocked["issue_type"], "bug")

    def test_show_of_a_missing_issue_fails_like_bd(self):
        code, out, err = self.cli("show", "t-nope")
        self.assertEqual((code, out), (1, ""))
        self.assertIn('no issue found matching "t-nope"', err)

    def test_show_takes_several_ids_like_bd(self):
        # bd 1.1.2, measured 2026-10-23 (workspace-7m8.31): `bd show A B` prints each, a rule
        # between them; --json is one array; a missing id is reported and skipped, exit 0.
        code, out, err = self.cli("show", "t-blocked", "t-nope", "t-epic.1", "--json")
        self.assertEqual((code, [i["id"] for i in json.loads(out)]), (0, ["t-blocked", "t-epic.1"]))
        self.assertIn('no issue found matching "t-nope"', err)
        code, text, _ = self.cli("show", "t-blocked", "t-epic.1")
        self.assertEqual(code, 0)
        first, second = text.split(f"\n\n{cli_module.SHOW_RULE}\n\n")
        self.assertTrue(first.startswith("○ t-blocked "))
        self.assertTrue(second.startswith("◐ t-epic.1 "))

    def test_show_fails_only_when_no_id_is_found(self):
        code, out, err = self.cli("show", "t-nope", "t-nope2", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.count("no issue found matching"), 2)


class RefusalTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.build(ROWS)

    def test_write_verbs_are_refused_in_shadow_mode(self):
        for argv in (["create", "--title", "x"], ["update", "t-epic", "--claim"],
                     ["close", "t-epic"], ["remember", "--key", "k", "v"],
                     ["dep", "add", "t-epic", "t-done"]):
            code, out, err = self.cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertIn("read-only shadow", err)
        _, out, _ = self.cli("list", "--all", "--json")
        self.assertEqual(len(json.loads(out)), 5)

    def test_ready_claim_is_a_write_and_is_refused(self):
        for argv in (["ready", "--claim"], ["ready", "--claim", "--json"],
                     ["--json", "ready", "-n", "1", "--claim"]):
            code, out, err = self.cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertIn("read-only shadow", err)
        _, out, _ = self.cli("list", "--status=in_progress", "--json")
        self.assertEqual([i["id"] for i in json.loads(out)], ["t-epic.1"])

    def test_ready_claim_is_refused_where_there_is_no_store(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            code, out, err = self.cli("ready", "--claim", cwd=Path(elsewhere))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no .cairn/ store found", err)

    def test_the_value_of_dash_C_is_not_mistaken_for_a_verb(self):
        odd = self.workspace / "create"
        odd.mkdir()
        code, out, _ = self.cli("stats", "--json", cwd=odd)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["issues"], 5)

    def test_bad_arguments_are_rejected(self):
        for argv, fragment in ((["ready", "-p", "9"], "invalid priority '9'"),
                               (["ready", "-p", "high"], "invalid priority 'high'"),
                               (["list", "--status=wontfix"], "unknown status 'wontfix'"),
                               (["list", "--limit", "-1"], "invalid limit -1")):
            code, out, err = self.cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertIn(fragment, err)

    def test_priority_accepts_both_spellings(self):
        for spelling in ("1", "P1", "p1"):
            _, out, _ = self.cli("ready", "-p", spelling, "--json")
            self.assertEqual([i["id"] for i in json.loads(out)], ["t-epic"])

    def test_an_internal_error_exits_2_with_a_message_not_a_traceback(self):
        from unittest import mock
        from cairn import cli
        with mock.patch.object(cli, "stats", side_effect=KeyError("surprise")):
            code, out, err = self.cli("stats")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("internal error: KeyError", err)
        self.assertNotIn("Traceback", err)

    def test_more_than_one_parent_is_a_check_error(self):
        self.build(ROWS + [with_deps(issue("t-two"), dep("t-two", "t-epic", "parent-child"),
                                     dep("t-two", "t-done", "parent-child"))])
        code, out, _ = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("✗ t-two has 2 parents (expected at most one)", out)

    def test_unknown_verb_is_a_usage_error(self):
        code, _, _ = self.cli("frobnicate")
        self.assertEqual(code, 2)


class DiscoveryTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.build(ROWS)

    def test_store_is_found_from_inside_a_nested_git_repo(self):
        nested = self.workspace / "nested" / "deep"
        nested.mkdir(parents=True)
        (self.workspace / "nested" / ".git").mkdir()
        self.assertEqual(find_store_dir(nested, env={}), self.store_dir.resolve())
        code, out, _ = self.cli("stats", "--json", cwd=nested)
        self.assertEqual((code, json.loads(out)["issues"]), (0, 5))

    def test_cairn_dir_overrides_the_walk(self):
        elsewhere = self.workspace / "elsewhere"
        self.assertEqual(find_store_dir(self.workspace, env={"CAIRN_DIR": str(elsewhere)}),
                         elsewhere)

    def test_store_path_with_uri_metacharacters(self):
        import shutil
        odd = self.workspace / "we ird#dir?x%41" / "project"
        odd.mkdir(parents=True)
        shutil.copytree(self.store_dir, odd / STORE_DIRNAME)
        code, out, err = self.cli("stats", "--json", cwd=odd)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["issues"], 5)

    def test_no_store_is_a_clear_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as empty:
            code, out, err = self.cli("ready", cwd=Path(empty))
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"no {STORE_DIRNAME}/ store found", err)

    def test_a_foreign_sqlite_file_is_not_mistaken_for_a_store(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as other:
            store = Path(other) / STORE_DIRNAME
            store.mkdir()
            conn = sqlite3.connect(store / "cairn.db")
            conn.execute("CREATE TABLE unrelated (x)")
            conn.commit()
            conn.close()
            code, _, err = self.cli("ready", cwd=Path(other))
        self.assertEqual(code, 2)
        self.assertIn("is not a cairn store", err)


class LauncherTest(StoreCase):
    def test_bin_launcher_runs_through_a_symlink(self):
        self.build(ROWS)
        launcher = Path(__file__).resolve().parents[1] / "bin" / "cairn"
        link = self.workspace / "cairn-link"
        os.symlink(launcher, link)
        proc = subprocess.run([sys.executable, str(link), "--json", "list",
                               "--status=in_progress"], cwd=self.workspace,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([i["id"] for i in json.loads(proc.stdout)], ["t-epic.1"])

    def test_a_closed_pipe_ends_quietly_like_bd(self):
        # `cairn ready --json | true`: the output fits Python's buffer, so the broken pipe
        # surfaced only at interpreter exit, as exit 120 and "Exception ignored ... BrokenPipeError"
        # (workspace-7m8.32). bd 1.1.2 exits 141 and prints nothing. The read end is closed
        # before the process starts, so the first write fails every time.
        self.build(ROWS)
        launcher = Path(__file__).resolve().parents[1] / "bin" / "cairn"
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run([sys.executable, str(launcher), "--json", "list", "--all"],
                                  cwd=self.workspace, stdout=write_end, stderr=subprocess.PIPE,
                                  text=True)
        finally:
            os.close(write_end)
        self.assertEqual((proc.returncode, proc.stderr), (141, ""))
