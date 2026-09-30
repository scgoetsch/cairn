"""End-to-end against a real `bd`, on a throwaway store.

Opt-in: set CAIRN_TEST_WITH_BD=1 (and have bd on PATH). It builds the edge cases
the semantics in cairn/queries.py were measured on, then requires the shadow
comparison to come back clean — so the measurements can be repeated against
whatever bd version is installed, instead of being trusted from a comment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cairn import shadow
from cairn.queries import Snapshot
from cairn.store import STORE_DIRNAME, db_path, open_db

ENABLED = os.environ.get("CAIRN_TEST_WITH_BD") == "1" and shutil.which("bd") is not None


@unittest.skipUnless(ENABLED, "set CAIRN_TEST_WITH_BD=1 with bd on PATH to run")
class RealBdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls._tmp.name)
        cls.sh("git", "init", "-q", ".")
        cls.sh("bd", "init", "--skip-agents", "--skip-hooks", "--non-interactive",
               "--sandbox", "-p", "t", "-q")
        new, dep, bd = cls.new, cls.dep, cls.bd

        x = new("blocker, open", "-p", "1")
        p = new("task blocked by an open blocker"); dep(p, x)
        c = new("child of a blocked parent", "--parent", p)
        new("grandchild of a blocked parent", "--parent", c)
        b1 = new("second blocker, open")
        q = new("child of a blocked parent with its own blocker", "--parent", p); dep(q, b1)

        g = new("in-progress blocker"); bd("update", g, "--claim")
        dep(new("blocked by an in-progress issue"), g)
        d = new("deferred blocker"); dep(new("blocked by a deferred issue"), d)
        bd("defer", d, "--until=+30d")
        m = new("closed blocker"); dep(new("blocked only by a closed issue"), m)
        bd("close", m)

        new("created with a future defer date", "--defer", "+30d")
        new("created with a past defer date", "--defer", "2020-01-01")
        dp = new("parent deferred to the future")
        dc = new("child of a future-deferred parent", "--parent", dp)
        new("grandchild of a future-deferred parent", "--parent", dc)
        bd("defer", dp, "--until=+30d")
        pp = new("parent deferred to the past")
        new("child of a past-deferred parent", "--parent", pp)
        bd("defer", pp, "--until=2020-01-01")
        cp = new("parent deferred to the future, then closed")
        new("child of a closed parent that keeps a future defer date", "--parent", cp)
        bd("defer", cp, "--until=+30d")
        bd("close", cp)

        j = new("origin of a discovery")
        new("discovered from another issue", "--deps", f"discovered-from:{j}")
        new("related to another issue", "--deps", f"related:{x}")
        ea = new("epic A", "-t", "epic")
        eb = new("epic B blocked by epic A", "-t", "epic"); dep(eb, ea)
        new("child of a blocked epic", "--parent", eb)
        for priority in ("0", "1", "1", "3", "4"):
            new(f"priority {priority}", "-p", priority)
        bd("remember", "--key", "first-memory", "a body that links to [[second-memory]]")
        bd("remember", "--key", "second-memory", "another body")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @classmethod
    def sh(cls, *argv: str) -> str:
        proc = subprocess.run(argv, cwd=cls.workspace, capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"{' '.join(argv)} failed: {proc.stderr or proc.stdout}")
        return proc.stdout.strip()

    @classmethod
    def bd(cls, *argv: str) -> str:
        return cls.sh("bd", *argv, "--sandbox")

    @classmethod
    def new(cls, title: str, *argv: str) -> str:
        return cls.bd("create", "--silent", "--title", title, *argv)

    @classmethod
    def dep(cls, issue: str, blocker: str) -> None:
        cls.bd("dep", "add", issue, blocker)

    def test_cairn_agrees_with_bd_on_every_measured_edge_case(self):
        store = self.workspace / STORE_DIRNAME
        result = shadow.run(store, self.workspace, "bd", deadline_seconds=120)
        self.assertEqual(result.differences, [])
        self.assertEqual(result.report.losses, [])

        conn = open_db(db_path(store))
        self.addCleanup(conn.close)
        snap = Snapshot(conn)
        # the scenario really contains what it is meant to exercise
        self.assertGreaterEqual(len(snap.blocked()), 8)
        self.assertGreaterEqual(len(snap.issues), 30)
        titles = {snap.issues[i]["title"] for i in snap.ready(now=shadow.timeutil.now_iso())}
        self.assertIn("grandchild of a future-deferred parent", titles)
        self.assertIn("child of a past-deferred parent", titles)
        self.assertIn("created with a past defer date", titles)
        self.assertNotIn("child of a future-deferred parent", titles)
        self.assertNotIn("child of a closed parent that keeps a future defer date", titles)
        self.assertNotIn("child of a blocked epic", titles)

        # `bd show` for every issue, not only the three a normal run samples
        deadline = shadow.Deadline(300)
        for offset in range(0, len(snap.issues), shadow.SHOW_SAMPLE):
            self.assertEqual(
                shadow.diff(store, self.workspace, "bd", deadline=deadline,
                            sample_offset=offset), [], f"show sample at offset {offset}")
