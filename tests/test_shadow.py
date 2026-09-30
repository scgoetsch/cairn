"""Shadow sync and diff, against a stub `bd` executable.

The stub answers from files the test writes by hand, so the expected answers
are independent of cairn's own queries. The point of most tests here is that
the diff can FAIL: a comparison that has only ever passed proves nothing.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

from cairn import cli, shadow

from .helpers import StoreCase, child, dep, issue, memory, with_deps, write_jsonl

STUB = r'''#!{python}
import json, os, shutil, sys, time
here = os.path.dirname(os.path.abspath(__file__))
answers = os.path.join(here, "answers")
with open(os.path.join(here, "cwd.txt"), "w") as handle:
    handle.write(os.getcwd())
argv = sys.argv[1:]
if os.path.exists(os.path.join(answers, "sleep")):
    time.sleep(float(open(os.path.join(answers, "sleep")).read()))
if argv[:3] == ["export", "--all", "-o"] and len(argv) == 4:
    shutil.copy(os.path.join(answers, "export.jsonl"), argv[3])
    sys.exit(0)
# Exact argument vectors: a dropped --limit 0, --all or --json must fail here,
# because against real bd it would silently change the answer.
EXPECTED = {{
    ("ready", "--limit", "0", "--json"): "ready",
    ("list", "--status=in_progress", "--limit", "0", "--json"): "in_progress",
    ("list", "--all", "--limit", "0", "--json"): "all",
    ("blocked", "--json"): "blocked",
    ("memories", "--json"): "memories",
}}
name = EXPECTED.get(tuple(argv))
if name is None and len(argv) == 3 and argv[0] == "show" and argv[2] == "--json":
    name = "show-" + argv[1]
if name is None:
    sys.stderr.write("stub bd: unexpected arguments: " + " ".join(argv) + "\n")
    sys.exit(4)
args = argv[:-1]
once = os.path.join(answers, (name or "missing") + ".once.json")
path = os.path.join(answers, (name or "missing") + ".json")
if os.path.exists(once):            # answered once, then gone: a write racing the run
    sys.stdout.write(open(once).read())
    os.remove(once)
    sys.exit(0)
if not os.path.exists(path):
    sys.stderr.write("stub bd: no answer for " + " ".join(args) + "\n")
    sys.exit(3)
sys.stdout.write(open(path).read())
'''

ROWS = [
    issue("t-x", priority=1),
    with_deps(issue("t-p"), dep("t-p", "t-x")),
    child("t-p.1", "t-p"),
    issue("t-go", status="in_progress"),
    issue("t-done", status="closed"),
    memory("k-one", "body one"),
    memory("k-two", "links to [[k-one]]"),
]


def bd_object(issue_id: str, *, blocks_out: int = 0, blocks_in: int = 0, **fields):
    """An issue as bd's `list --json` prints it, written out by hand: the counts
    are given, not computed, so they are independent of cairn's counting."""
    obj = issue(issue_id, **fields)
    del obj["_type"]
    obj.update(dependency_count=blocks_out, dependent_count=blocks_in, comment_count=0)
    return obj


def nested(issue_id: str, dep_type: str, **fields):
    obj = issue(issue_id, **fields)
    del obj["_type"]
    obj["dependency_type"] = dep_type
    return obj


def answers():
    """What a correct bd would answer for ROWS."""
    listed = [
        bd_object("t-x", priority=1, blocks_in=1),
        bd_object("t-p", blocks_out=1, dependencies=[dep("t-p", "t-x")]),
        bd_object("t-p.1", parent="t-p", dependencies=[dep("t-p.1", "t-p", "parent-child")]),
        bd_object("t-go", status="in_progress"),
        bd_object("t-done", status="closed"),
    ]
    shown = {
        # `show` counts edges of every type, and expands dependencies
        "t-x": bd_object("t-x", priority=1, blocks_in=1),
        "t-p": bd_object("t-p", blocks_out=1, blocks_in=1,
                         dependencies=[nested("t-x", "blocks", priority=1)]),
        "t-p.1": bd_object("t-p.1", blocks_out=1, parent="t-p",
                           dependencies=[nested("t-p", "parent-child")]),
        "t-go": bd_object("t-go", status="in_progress"),
        "t-done": bd_object("t-done", status="closed"),
    }
    out = {
        "ready": [{"id": "t-x"}],
        "in_progress": [{"id": "t-go"}],
        "blocked": [{"id": "t-p", "blocked_by": ["t-x"]},
                    {"id": "t-p.1", "blocked_by": ["t-p"]}],
        "all": listed,
        "memories": {"k-one": "body one", "k-two": "links to [[k-one]]", "schema_version": 1},
    }
    out.update({f"show-{k}": [v] for k, v in shown.items()})
    return out


class ShadowCase(StoreCase):
    def setUp(self):
        super().setUp()
        (self.workspace / ".beads").mkdir()
        (self.workspace / ".claude").mkdir()
        # the three optional inputs, present and empty
        (self.workspace / ".beads" / "interactions.jsonl").write_text("", encoding="utf-8")
        (self.workspace / ".beads" / "memgraph-ledger.json").write_text("{}", encoding="utf-8")
        (self.workspace / ".claude" / "memory-hot.txt").write_text("", encoding="utf-8")
        self.stub_dir = self.workspace / "stub"
        (self.stub_dir / "answers").mkdir(parents=True)
        self.bd = self.stub_dir / "bd"
        self.bd.write_text(STUB.format(python=sys.executable), encoding="utf-8")
        self.bd.chmod(self.bd.stat().st_mode | stat.S_IXUSR)
        write_jsonl(self.stub_dir / "answers" / "export.jsonl", ROWS)
        self.answers = answers()
        for name, answer in self.answers.items():
            self.answer(name, answer)

    def answer(self, name: str, value) -> None:
        (self.stub_dir / "answers" / f"{name}.json").write_text(
            json.dumps(value), encoding="utf-8")

    def run_shadow(self, **kwargs):
        return shadow.run(self.store_dir, self.workspace, str(self.bd), **kwargs)

    def shadow_cli(self, *argv: str, **kwargs):
        return self.cli("shadow", *argv, "--bd", str(self.bd), **kwargs)

    def log(self):
        return [json.loads(line) for line in
                (self.store_dir / shadow.LOG_NAME).read_text().splitlines()]


class AgreementTest(ShadowCase):
    def test_clean_run_reports_nothing_and_logs_ok(self):
        result = self.run_shadow()
        self.assertEqual((result.differences, result.report.losses, result.report.warnings),
                         ([], [], []))
        self.assertTrue(result.clean)
        self.assertFalse(result.retried)
        self.assertEqual(result.report.counts["issues"], 5)
        entry = self.log()[-1]
        self.assertIs(entry["ok"], True)
        self.assertEqual((entry["issues"], entry["memories"]), (5, 2))

    def test_cli_quiet_run_prints_nothing_when_clean(self):
        self.assertEqual(self.shadow_cli("run", "--quiet"), (0, "", ""))

    def test_first_run_creates_a_git_ignored_store_beside_beads(self):
        self.assertFalse(self.store_dir.exists())
        self.shadow_cli("run", "--quiet")
        self.assertTrue((self.store_dir / "cairn.db").is_file())
        self.assertEqual((self.store_dir / ".gitignore").read_text(),
                         "*\n!.gitignore\n!config.json\n")
        leftovers = [p.name for p in self.store_dir.iterdir() if ".tmp-" in p.name]
        self.assertEqual(leftovers, [])

    def test_bd_runs_in_the_beads_workspace_even_when_the_store_is_elsewhere(self):
        elsewhere = Path(tempfile.mkdtemp(dir=self.workspace)) / "custom-store"
        with mock.patch.dict(os.environ, {"CAIRN_DIR": str(elsewhere)}):
            code, out, err = self.shadow_cli("run", "--quiet")
            self.assertEqual((code, out, err), (0, "", ""))
            self.assertEqual(json.loads(self.cli("stats", "--json")[1])["issues"], 5)
        self.assertTrue((elsewhere / "cairn.db").is_file())
        self.assertFalse(self.store_dir.exists())
        self.assertEqual(Path((self.stub_dir / "cwd.txt").read_text()).resolve(),
                         self.workspace.resolve())

    def test_issue_prefixes_in_the_store_config_survive_every_sync(self):
        rows = ROWS + [memory("k-ref", "see [[WS-abc]]")]
        write_jsonl(self.stub_dir / "answers" / "export.jsonl", rows)
        self.answer("memories", dict(self.answers["memories"], **{"k-ref": "see [[WS-abc]]"}))
        self.shadow_cli("run", "--quiet")
        code, out, _ = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("dangling link: memory k-ref -related-> WS-abc", out)
        (self.store_dir / "config.json").write_text('{"issue_prefixes": ["ws"]}',
                                                    encoding="utf-8")
        self.assertEqual(self.shadow_cli("run", "--quiet"), (0, "", ""))
        code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertIn("refers to issue WS-abc, which is not in this store", out)

    def test_a_malformed_store_config_stops_the_run_loudly(self):
        self.shadow_cli("run", "--quiet")
        (self.store_dir / "config.json").write_text('{"issue_prefixes": "ws"}',
                                                    encoding="utf-8")
        code, out, err = self.shadow_cli("run", "--quiet")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("'issue_prefixes' must be a list of strings", err)
        self.assertIsNone(self.log()[-1]["ok"])

    def test_an_absent_optional_input_is_said_not_assumed_empty(self):
        (self.workspace / ".beads" / "interactions.jsonl").unlink()
        (self.workspace / ".beads" / "memgraph-ledger.json").unlink()
        (self.workspace / ".claude" / "memory-hot.txt").unlink()
        result = self.run_shadow()
        self.assertTrue(result.clean)
        self.assertEqual(result.report.warnings, [
            "no .beads/interactions.jsonl — history was not imported",
            "no .beads/memgraph-ledger.json — memories have no timestamps or deletion records",
            "no .claude/memory-hot.txt — no memory is in the hot tier",
        ])
        self.assertEqual(self.shadow_cli("run", "--quiet"), (0, "", ""))

    def test_temp_files_left_by_a_killed_run_are_cleared_but_young_ones_are_not(self):
        self.store_dir.mkdir()
        old_export = self.store_dir / "bd-export.tmp-111-aaaa.jsonl"
        old_db = self.store_dir / "cairn.db.tmp-111-bbbb"
        young = self.store_dir / "bd-export.tmp-222-cccc.jsonl"
        for path in (old_export, old_db, young):
            path.write_text("left behind", encoding="utf-8")
        hour_ago = __import__("time").time() - 3600
        os.utime(old_export, (hour_ago, hour_ago))
        os.utime(old_db, (hour_ago, hour_ago))
        self.run_shadow()
        self.assertFalse(old_export.exists())
        self.assertFalse(old_db.exists())
        self.assertTrue(young.exists())

    def test_sigterm_cleans_up_instead_of_leaving_the_export_behind(self):
        import signal
        import subprocess
        import time
        (self.stub_dir / "answers" / "sleep").write_text("20", encoding="utf-8")
        launcher = Path(__file__).resolve().parents[1] / "bin" / "cairn"
        proc = subprocess.Popen(
            [sys.executable, str(launcher), "shadow", "run", "--quiet", "--bd", str(self.bd),
             "--deadline", "60"], cwd=self.workspace,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while not (self.stub_dir / "cwd.txt").exists() and time.time() < deadline:
            time.sleep(0.05)                      # wait until cairn is inside a bd call
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        self.assertEqual([p.name for p in self.store_dir.iterdir() if ".tmp-" in p.name], [])

    def test_a_write_racing_the_run_is_retried_not_reported(self):
        (self.stub_dir / "answers" / "ready.once.json").write_text("[]", encoding="utf-8")
        result = self.run_shadow()
        self.assertEqual(result.differences, [])
        self.assertTrue(result.retried)
        entry = self.log()[-1]
        self.assertIs(entry["ok"], True)
        self.assertIs(entry["retried"], True)
        _, out, _ = self.shadow_cli("status")
        self.assertIn("Current clean streak: 1 runs", out)

    def test_oddities_in_the_source_data_are_warnings_and_do_not_fail_the_run(self):
        hot = self.workspace / ".claude" / "memory-hot.txt"
        hot.write_text("k-one\nk-missing\n", encoding="utf-8")
        self.assertEqual(self.shadow_cli("run", "--quiet"), (0, "", ""))
        code, out, _ = self.shadow_cli("run")
        self.assertEqual(code, 0)
        self.assertIn("WARN hot list names a memory that does not exist: k-missing", out)
        self.assertIn("✓ cairn agrees with bd (run)", out)
        self.assertIs(self.log()[-1]["ok"], True)
        self.assertEqual(self.log()[-1]["warnings"], 1)


class DisagreementTest(ShadowCase):
    def differences(self):
        result = self.run_shadow()
        self.assertTrue(result.retried, "a difference must survive a second attempt")
        self.assertIs(self.log()[-1]["ok"], False)
        return result.differences

    def test_ready_set(self):
        self.answer("ready", [{"id": "t-x"}, {"id": "t-p"}])
        self.assertEqual(self.differences(), ["ready: only in bd: ['t-p']"])

    def test_ready_order(self):
        rows = ROWS + [issue("t-y", priority=1, created_at="2026-09-11T10:00:00Z")]
        write_jsonl(self.stub_dir / "answers" / "export.jsonl", rows)
        newer = bd_object("t-y", priority=1, created_at="2026-09-11T10:00:00Z")
        self.answer("all", self.answers["all"] + [newer])
        self.answer("show-t-y", [newer])
        self.answer("ready", [{"id": "t-x"}, {"id": "t-y"}])      # cairn says newest first
        self.assertEqual(self.differences(),
                         ["ready order: differs from position 0 (bd t-x, cairn t-y)"])

    def test_ready_order_is_still_checked_when_the_sets_differ(self):
        rows = ROWS + [issue("t-y", priority=1, created_at="2026-09-11T10:00:00Z")]
        write_jsonl(self.stub_dir / "answers" / "export.jsonl", rows)
        newer = bd_object("t-y", priority=1, created_at="2026-09-11T10:00:00Z")
        self.answer("all", self.answers["all"] + [newer])
        self.answer("show-t-y", [newer])
        self.answer("ready", [{"id": "t-x"}, {"id": "t-y"}, {"id": "t-p"}])
        self.assertEqual(self.differences(), [
            "ready: only in bd: ['t-p']",
            "ready order: differs from position 0 (bd t-x, cairn t-y)",
        ])

    def test_repeated_ids_in_bds_answer_are_reported_not_crashed_on(self):
        self.answer("ready", [{"id": "t-x"}, {"id": "t-x"}])
        self.assertEqual(self.differences(),
                         ["ready: bd lists these ids more than once: ['t-x']"])

    def test_in_progress_set(self):
        self.answer("in_progress", [])
        self.assertEqual(self.differences(), ["in_progress: only in cairn: ['t-go']"])

    def test_blocked_set_and_blockers(self):
        self.answer("blocked", [{"id": "t-p", "blocked_by": ["t-go"]}])
        self.assertEqual(self.differences(), [
            "blocked: only in cairn: ['t-p.1']",
            "blocked_by of t-p: bd ['t-go'], cairn ['t-x']",
        ])

    def test_any_field_of_any_issue(self):
        listed = {o["id"]: o for o in self.answers["all"]}
        del listed["t-done"]
        listed["t-extra"] = bd_object("t-extra")
        listed["t-x"] = dict(listed["t-x"], status="closed", title="renamed in bd")
        listed["t-p"] = bd_object("t-p")                       # no edge, count 0
        listed["t-go"] = dict(listed["t-go"], labels=["urgent"], priority=0)
        self.answer("all", list(listed.values()))
        self.assertEqual(self.differences(), [
            "issues: only in bd: ['t-extra']",
            "issues: only in cairn: ['t-done']",
            "issue t-go: bd and cairn differ in ['labels', 'priority']",
            "issue t-p: bd and cairn differ in ['dependencies', 'dependency_count']",
            "issue t-x: bd and cairn differ in ['status', 'title']",
        ])

    def test_many_differing_issues_are_summarised(self):
        rows = [issue(f"t-{n:02d}") for n in range(14)]
        write_jsonl(self.stub_dir / "answers" / "export.jsonl", rows)
        self.answer("all", [bd_object(f"t-{n:02d}", title="other") for n in range(14)])
        self.answer("ready", [{"id": f"t-{n:02d}"} for n in range(14)])
        self.answer("in_progress", [])
        self.answer("blocked", [])
        self.answer("memories", {"schema_version": 1})
        for n in range(14):
            self.answer(f"show-t-{n:02d}", [bd_object(f"t-{n:02d}")])
        differences = self.differences()
        self.assertEqual(len(differences), shadow.MAX_REPORTED + 1)
        self.assertEqual(differences[-1], "… and 4 more issues differing")

    def test_show_is_sampled_and_the_sample_rotates(self):
        wrong = dict(self.answers["show-t-x"][0], dependent_count=7)
        self.answer("show-t-x", [wrong])
        # five issues, three per run: the first run looks at t-done, t-go, t-p
        first = self.run_shadow()
        self.assertEqual(first.differences, [])
        second = self.run_shadow()                              # t-p.1, t-x, t-done
        self.assertEqual(second.differences,
                         ["show t-x: bd and cairn differ in ['dependent_count']"])

    def test_memory_keys_and_bodies(self):
        self.answer("memories", {"k-one": "body one CHANGED", "k-three": "new",
                                 "schema_version": 1})
        self.assertEqual(self.differences(), [
            "memory keys: only in bd: ['k-three']",
            "memory keys: only in cairn: ['k-two']",
            "memory body differs: k-one",
        ])

    def test_cli_prints_differences_and_exits_nonzero_even_when_quiet(self):
        self.answer("in_progress", [])
        code, out, _ = self.shadow_cli("run", "--quiet")
        self.assertEqual((code, out), (1, "DIFF in_progress: only in cairn: ['t-go']\n"))

    def test_rows_left_out_of_the_import_make_a_run_unclean(self):
        rows = ROWS + [{"_type": "gate", "id": "g-1"}]
        write_jsonl(self.stub_dir / "answers" / "export.jsonl", rows)
        code, out, _ = self.shadow_cli("run", "--quiet")
        self.assertEqual((code, out),
                         (1, "LOSS 1 row(s) of unsupported _type 'gate' were not imported\n"))
        entry = self.log()[-1]
        self.assertIs(entry["ok"], False)
        self.assertEqual(entry["losses"], 1)


class RoundTripTest(ShadowCase):
    def test_a_field_lost_between_import_and_export_is_reported(self):
        self.run_shadow()
        export = self.stub_dir / "answers" / "export.jsonl"
        conn = __import__("cairn.store", fromlist=["open_db"]).open_db(
            self.store_dir / "cairn.db")
        self.addCleanup(conn.close)
        self.assertEqual(shadow.roundtrip_differences(conn, export), [])
        tampered = [dict(r) for r in ROWS]
        tampered[0]["notes"] = "present in bd, absent from the store"
        tampered.append(issue("t-never-imported"))
        tampered[-2]["value"] = "a different body"
        write_jsonl(export, tampered)
        self.assertEqual(shadow.roundtrip_differences(conn, export), [
            "round trip: issue t-x differs in ['notes']",
            "round trip: memory k-two differs",
            "round trip: issue t-never-imported is missing from the store",
        ])


class FailureTest(ShadowCase):
    """A run that could not happen must be loud, exit 2, and count as neither
    clean nor failed."""

    def assert_could_not_run(self, fragment: str):
        entry = self.log()[-1]
        self.assertIsNone(entry["ok"])
        self.assertIn(fragment, entry["error"])
        data = shadow.status(self.store_dir)
        self.assertEqual((data["could_not_run"], data["failed_runs"]), (1, 0))

    def test_bd_missing(self):
        code, out, err = self.cli("shadow", "run", "--quiet", "--bd", "no-such-bd-binary")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("'no-such-bd-binary' is not on PATH", err)
        self.assert_could_not_run("is not on PATH")

    def test_bd_command_failing(self):
        (self.stub_dir / "answers" / "blocked.json").unlink()
        code, _, err = self.shadow_cli("run", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("bd blocked --json exited 3: stub bd: no answer for blocked", err)
        self.assert_could_not_run("exited 3")

    def test_bd_printing_non_json(self):
        (self.stub_dir / "answers" / "ready.json").write_text("not json", encoding="utf-8")
        code, _, err = self.shadow_cli("run", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("did not print JSON", err)

    def test_bd_hanging_past_the_deadline(self):
        (self.stub_dir / "answers" / "sleep").write_text("5", encoding="utf-8")
        code, out, err = self.shadow_cli("run", "--quiet", "--deadline", "0.4")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("0.4s deadline", err)
        self.assert_could_not_run("deadline")

    def test_bd_answering_in_an_unexpected_shape(self):
        for name, value, fragment in (
                ("ready", {"not": "a list"}, "bd ready --json did not print a list of issues"),
                ("all", [{"no": "id"}], "bd list --all --json did not print a list of issues"),
                ("memories", ["a", "list"], "bd memories --json did not print an object"),
                ("memories", {"k-one": 7, "schema_version": 1},
                 "printed a value that is not a string")):
            for answer_name, answer in self.answers.items():
                self.answer(answer_name, answer)
            self.answer(name, value)
            code, out, err = self.shadow_cli("run", "--quiet")
            self.assertEqual((code, out), (2, ""), name)
            self.assertIn(fragment, err)
            self.assertIsNone(self.log()[-1]["ok"])

    def test_the_stub_rejects_a_query_with_a_flag_missing(self):
        # guards the guard: the stub must not accept `bd ready` without --limit 0
        import subprocess
        for argv in (["ready", "--json"], ["list", "--all", "--json"],
                     ["list", "--all", "--limit", "0"], ["memories"]):
            proc = subprocess.run([str(self.bd), *argv], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 4, argv)

    def test_malformed_export_keeps_the_previous_store(self):
        self.run_shadow()
        (self.stub_dir / "answers" / "export.jsonl").write_text("{broken\n", encoding="utf-8")
        code, _, err = self.shadow_cli("sync")
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        code, out, _ = self.cli("stats", "--json")
        self.assertEqual(json.loads(out)["issues"], 5)

    def test_no_beads_store_anywhere(self):
        with tempfile.TemporaryDirectory() as empty:
            code, _, err = self.cli("shadow", "run", cwd=Path(empty))
        self.assertEqual(code, 2)
        self.assertIn("no .beads/ store found", err)

    def test_a_crash_exits_2_not_1(self):
        # exit 1 means "differences found"; an internal error must not look like that
        with mock.patch.object(cli.shadow, "run", side_effect=RuntimeError("boom")):
            code, out, err = self.shadow_cli("run", "--quiet")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("internal error: RuntimeError: boom", err)


class SoakTest(ShadowCase):
    def write_log(self, entries, raw_tail: str = ""):
        self.store_dir.mkdir(exist_ok=True)
        (self.store_dir / shadow.LOG_NAME).write_text(
            "".join(json.dumps(e) + "\n" for e in entries) + raw_tail, encoding="utf-8")

    def test_no_runs_yet(self):
        data = shadow.status(self.store_dir)
        self.assertEqual((data["runs"], data["clean_streak"], data["criteria_met"]),
                         (0, 0, False))

    def test_a_failure_resets_the_streak(self):
        self.write_log(
            [{"at": f"2026-09-{d:02d}T08:00:00Z", "ok": True} for d in range(1, 13)]
            + [{"at": "2026-09-13T08:00:00Z", "ok": False, "first": ["ready: only in bd: ['t']"]}]
            + [{"at": f"2026-09-{d:02d}T08:00:00Z", "ok": True} for d in range(14, 17)])
        data = shadow.status(self.store_dir)
        self.assertEqual((data["runs"], data["failed_runs"], data["clean_streak"]), (16, 1, 3))
        self.assertEqual(data["clean_streak_since"], "2026-09-14T08:00:00Z")
        self.assertFalse(data["criteria_met"])

    def test_a_run_that_could_not_happen_neither_counts_nor_resets(self):
        clean = [{"at": f"2026-09-{d:02d}T08:00:00Z", "ok": True} for d in range(1, 17)]
        errored = {"at": "2026-09-08T09:00:00Z", "ok": None, "error": "'bd' is not on PATH"}
        self.write_log(clean[:8] + [errored] + clean[8:])
        data = shadow.status(self.store_dir)
        self.assertEqual((data["runs"], data["clean_streak"], data["could_not_run"]),
                         (17, 16, 1))
        self.assertTrue(data["criteria_met"])
        self.assertEqual(data["last_error"]["error"], "'bd' is not on PATH")

    def test_ten_clean_runs_in_one_day_are_not_enough(self):
        self.write_log([{"at": f"2026-09-01T{h:02d}:00:00Z", "ok": True} for h in range(12)])
        data = shadow.status(self.store_dir)
        self.assertEqual(data["clean_streak"], 12)
        self.assertFalse(data["criteria_met"])

    def test_two_weeks_with_too_few_runs_are_not_enough(self):
        self.write_log([{"at": "2026-09-01T08:00:00Z", "ok": True},
                        {"at": "2026-09-20T08:00:00Z", "ok": True}])
        self.assertFalse(shadow.status(self.store_dir)["criteria_met"])

    def test_ten_clean_runs_over_two_weeks_meet_the_criteria(self):
        self.write_log([{"at": f"2026-09-{d:02d}T08:00:00Z", "ok": True}
                        for d in range(1, 17)])
        data = shadow.status(self.store_dir)
        self.assertEqual((data["clean_streak"], data["clean_streak_days"]), (16, 15.0))
        self.assertTrue(data["criteria_met"])

    def test_an_hour_short_of_two_weeks_is_not_two_weeks(self):
        def log_spanning(last):
            runs = [{"at": f"2026-09-{d:02d}T08:00:00Z", "ok": True} for d in range(1, 12)]
            return runs + [{"at": last, "ok": True}]
        self.write_log(log_spanning("2026-09-15T07:00:00Z"))       # 13 days 23 hours
        data = shadow.status(self.store_dir)
        self.assertEqual((data["clean_streak"], data["clean_streak_days"]), (12, 14.0))
        self.assertFalse(data["criteria_met"])
        self.write_log(log_spanning("2026-09-15T08:00:00Z"))       # exactly 14 days
        self.assertTrue(shadow.status(self.store_dir)["criteria_met"])

    def test_a_damaged_log_line_is_counted_not_fatal(self):
        self.write_log([{"at": "2026-09-01T08:00:00Z", "ok": True}],
                       raw_tail='{"at": "2026-09-02T08:00:00Z", "ok": tr\n'
                                '{"ok": true}\n["not", "an", "entry"]\n')
        data = shadow.status(self.store_dir)
        self.assertEqual((data["runs"], data["unreadable_log_lines"]), (1, 3))
        code, out, _ = self.cli("shadow", "status")
        self.assertEqual(code, 0)
        self.assertIn("Unreadable log lines skipped: 3", out)
