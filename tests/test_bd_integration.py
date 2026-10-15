"""End-to-end against a real `bd`, on a throwaway store.

Opt-in: set CAIRN_TEST_WITH_BD=1 (and have bd on PATH). It builds the edge cases
the semantics in cairn/queries.py were measured on, then requires the shadow
comparison to come back clean — so the measurements can be repeated against
whatever bd version is installed, instead of being trusted from a comment.

IdAllocationTest pins how bd hands out issue ids. cairn allocates none yet; these
are the facts a writer has to be designed around.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

from cairn import shadow
from cairn.queries import Snapshot
from cairn.store import STORE_DIRNAME, db_path, open_db

from .bdversion import CREATE_ID_REFUSED, STRICT_CLOSE, unexplained

ENABLED = os.environ.get("CAIRN_TEST_WITH_BD") == "1" and shutil.which("bd") is not None


class ThrowawayBdStore(unittest.TestCase):
    """A fresh bd store with prefix `t` in a temp directory, one per class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls._tmp.name)
        cls.sh("git", "init", "-q", ".")
        cls.sh("bd", "init", "--skip-agents", "--skip-hooks", "--non-interactive",
               "--sandbox", "-p", "t", "-q")

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
    def bd_status(cls, *argv: str) -> Tuple[int, str]:
        """For commands that are expected to fail: exit status and stderr."""
        proc = subprocess.run(["bd", *argv, "--sandbox"], cwd=cls.workspace,
                              capture_output=True, text=True)
        return proc.returncode, proc.stderr

    @classmethod
    def new(cls, title: str, *argv: str) -> str:
        return cls.bd("create", "--silent", "--title", title, *argv)

    @classmethod
    def dep(cls, issue: str, blocker: str) -> None:
        cls.bd("dep", "add", issue, blocker)

    @classmethod
    def show(cls, issue_id: str) -> Dict[str, Any]:
        return json.loads(cls.bd("show", issue_id, "--json"))[0]

    @classmethod
    def exported_ids(cls) -> List[str]:
        rows = [json.loads(line) for line in cls.bd("export", "--all").splitlines()]
        return [row["id"] for row in rows if "id" in row]


@unittest.skipUnless(ENABLED, "set CAIRN_TEST_WITH_BD=1 with bd on PATH to run")
class RealBdTest(ThrowawayBdStore):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
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
        bd("close", cp, *(["--force"] if STRICT_CLOSE else []))

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

    def test_cairn_agrees_with_bd_on_every_measured_edge_case(self):
        store = self.workspace / STORE_DIRNAME
        result = shadow.run(store, self.workspace, "bd", deadline_seconds=120)
        self.assertEqual(unexplained(result.differences), [])
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
                unexplained(shadow.diff(store, self.workspace, "bd", deadline=deadline,
                                        sample_offset=offset)),
                [], f"show sample at offset {offset}")


@unittest.skipUnless(ENABLED, "set CAIRN_TEST_WITH_BD=1 with bd on PATH to run")
class IdAllocationTest(ThrowawayBdStore):
    """How bd 1.1.2 allocates issue ids, measured 2026-10-02. One test, because
    the child counter is state that each step builds on."""

    def test_id_allocation_as_measured(self):
        new, bd = self.new, self.bd

        # Root ids: prefix, a dash, three characters of [0-9a-z] in a small store.
        # The id is not a function of the title.
        roots = [new("same title") for _ in range(6)]
        self.assertEqual(len(set(roots)), 6)
        for issue_id in roots:
            self.assertRegex(issue_id, r"^t-[0-9a-z]{3}$")

        # Children are numbered from 1 under the parent's id, to any depth.
        parent = new("parent")
        self.assertEqual([new(f"child {n}", "--parent", parent) for n in (1, 2, 3)],
                         [f"{parent}.1", f"{parent}.2", f"{parent}.3"])
        self.assertEqual(new("grandchild", "--parent", f"{parent}.1"), f"{parent}.1.1")
        self.assertEqual(new("great-grandchild", "--parent", f"{parent}.1.1"),
                         f"{parent}.1.1.1")

        # A child number is never issued twice. Deleting the highest child, or one
        # in the middle, does not free its number — and the deleted ids are not in
        # the export, so "highest surviving child + 1" computed from an export
        # would reissue an id that commits and documents may still name.
        bd("delete", f"{parent}.3", "--force")
        self.assertEqual(new("after deleting the highest", "--parent", parent), f"{parent}.4")
        bd("delete", f"{parent}.2", "--force")
        self.assertEqual(new("after deleting one in the middle", "--parent", parent),
                         f"{parent}.5")
        exported = self.exported_ids()
        self.assertNotIn(f"{parent}.2", exported)
        self.assertNotIn(f"{parent}.3", exported)

        # A caller may choose a root id; bd checks the prefix and nothing else.
        self.assertEqual(new("chosen root", "--id", "t-zzz9"), "t-zzz9")
        code, err = self.bd_status("create", "--silent", "--title", "x", "--id", "other-abc")
        self.assertEqual(code, 1)
        self.assertIn("prefix mismatch", err)

        # A caller cannot choose a child id AND get the parent link: the two flags
        # exclude each other, and a child-shaped id given alone is created with no
        # parent, though it still moves the parent's counter past its number.
        code, err = self.bd_status("create", "--silent", "--title", "x",
                                   "--id", f"{parent}.9", "--parent", parent)
        self.assertEqual(code, 1)
        self.assertIn("cannot specify both --id and --parent", err)
        self.assertEqual(new("child-shaped id, no parent flag", "--id", f"{parent}.12"),
                         f"{parent}.12")
        self.assertFalse(self.show(f"{parent}.12").get("parent"))
        self.assertEqual(new("next numbered child", "--parent", parent), f"{parent}.13")

        # Creating with an id that is taken does not fail. It REPLACES the issue:
        # exit 0, same number of rows, the old description gone, a closed issue
        # open again. An allocator that collides loses data silently.
        new("first holder", "--id", "t-dup1", "--description", "original body", "-p", "1")
        bd("close", "t-dup1")
        before = len(self.exported_ids())
        if CREATE_ID_REFUSED:       # bd >= 1.3: refused, the holder untouched
            code, err = self.bd_status("create", "--silent", "--title", "second holder",
                                       "--id", "t-dup1")
            self.assertNotEqual(code, 0)
            self.assertIn("already exists", err)
            self.assertEqual(self.show("t-dup1")["title"], "first holder")
        else:
            self.assertEqual(new("second holder", "--id", "t-dup1"), "t-dup1")
            self.assertEqual(len(self.exported_ids()), before)
            holder = self.show("t-dup1")
            self.assertEqual(holder["title"], "second holder")
            self.assertFalse(holder.get("description"))
            self.assertEqual((holder["status"], holder["priority"]), ("open", 2))

        # cairn reads the result the way bd does, child-shaped orphan included.
        result = shadow.run(self.workspace / STORE_DIRNAME, self.workspace, "bd",
                            deadline_seconds=120)
        self.assertEqual(unexplained(result.differences), [])
        self.assertEqual(result.report.losses, [])
