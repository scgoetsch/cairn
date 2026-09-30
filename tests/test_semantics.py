"""ready / blocked semantics. Each case reproduces a behaviour measured on a
throwaway bd 1.1.2 store on 2026-09-30 (see cairn/queries.py)."""

from __future__ import annotations

from cairn.queries import Snapshot

from .helpers import FUTURE, NOW, PAST, StoreCase, child, dep, issue, with_deps


class BlockedTest(StoreCase):
    def snap(self, rows):
        conn, _ = self.build(rows)
        return Snapshot(conn)

    def test_open_blocker_blocks(self):
        snap = self.snap([issue("t-x"), with_deps(issue("t-a"), dep("t-a", "t-x"))])
        self.assertEqual(snap.blocked(), {"t-a": ["t-x"]})
        self.assertEqual(snap.ready(now=NOW), ["t-x"])

    def test_in_progress_and_deferred_blockers_still_block(self):
        snap = self.snap([
            issue("t-g", status="in_progress"),
            issue("t-d", status="deferred", defer_until=FUTURE),
            with_deps(issue("t-h"), dep("t-h", "t-g")),
            with_deps(issue("t-e"), dep("t-e", "t-d")),
        ])
        self.assertEqual(snap.blocked(), {"t-h": ["t-g"], "t-e": ["t-d"]})

    def test_closed_blocker_does_not_block(self):
        snap = self.snap([issue("t-m", status="closed"),
                          with_deps(issue("t-l"), dep("t-l", "t-m"))])
        self.assertEqual(snap.blocked(), {})
        self.assertEqual(snap.ready(now=NOW), ["t-l"])

    def test_related_and_discovered_from_never_block(self):
        snap = self.snap([
            issue("t-j"),
            with_deps(issue("t-i"), dep("t-i", "t-j", "discovered-from")),
            with_deps(issue("t-r"), dep("t-r", "t-j", "related")),
        ])
        self.assertEqual(snap.blocked(), {})
        self.assertEqual(set(snap.ready(now=NOW)), {"t-i", "t-j", "t-r"})

    def test_blocked_parent_blocks_descendants_transitively(self):
        snap = self.snap([
            issue("t-x"),
            with_deps(issue("t-p"), dep("t-p", "t-x")),
            child("t-p.1", "t-p"),
            child("t-p.1.1", "t-p.1"),
        ])
        self.assertEqual(snap.blocked(), {
            "t-p": ["t-x"], "t-p.1": ["t-p"], "t-p.1.1": ["t-p.1"]})
        self.assertEqual(snap.ready(now=NOW), ["t-x"])

    def test_open_parent_does_not_block_children(self):
        snap = self.snap([issue("t-p", issue_type="epic"), child("t-p.1", "t-p")])
        self.assertEqual(snap.blocked(), {})

    def test_own_blocker_wins_over_blocked_parent_in_blocked_by(self):
        snap = self.snap([
            issue("t-x"), issue("t-b"),
            with_deps(issue("t-p"), dep("t-p", "t-x")),
            with_deps(issue("t-p.2"), dep("t-p.2", "t-p", "parent-child"), dep("t-p.2", "t-b")),
        ])
        self.assertEqual(snap.blocked()["t-p.2"], ["t-b"])

    def test_in_progress_and_deferred_issues_can_be_blocked(self):
        snap = self.snap([
            issue("t-b"),
            with_deps(issue("t-ip", status="in_progress"), dep("t-ip", "t-b")),
            with_deps(issue("t-df", status="deferred", defer_until=FUTURE), dep("t-df", "t-b")),
        ])
        self.assertEqual(set(snap.blocked()), {"t-ip", "t-df"})

    def test_closed_issue_is_never_blocked(self):
        snap = self.snap([issue("t-b"),
                          with_deps(issue("t-c", status="closed"), dep("t-c", "t-b"))])
        self.assertEqual(snap.blocked(), {})

    def test_dangling_blocker_does_not_block(self):
        conn, report = self.build([with_deps(issue("t-a"), dep("t-a", "t-gone"))])
        self.assertEqual(Snapshot(conn).blocked(), {})
        self.assertTrue(any("dangling dependency" in w for w in report.warnings))


class ReadyTest(StoreCase):
    def snap(self, rows):
        conn, _ = self.build(rows)
        return Snapshot(conn)

    def test_only_status_open_is_ready(self):
        snap = self.snap([
            issue("t-o"), issue("t-ip", status="in_progress"),
            issue("t-c", status="closed"), issue("t-d", status="deferred", defer_until=FUTURE),
        ])
        self.assertEqual(snap.ready(now=NOW), ["t-o"])

    def test_deferred_status_never_resurfaces_even_after_its_date(self):
        # bd quirk: the issue stays hidden; cairn lists it under due_deferred instead.
        snap = self.snap([issue("t-d", status="deferred", defer_until=PAST)])
        self.assertEqual(snap.ready(now=NOW), [])
        self.assertEqual(snap.ready(now=NOW, include_deferred=True), [])
        self.assertEqual(snap.due_deferred(NOW), ["t-d"])

    def test_future_deferral_is_not_due(self):
        snap = self.snap([issue("t-d", status="deferred", defer_until=FUTURE)])
        self.assertEqual(snap.due_deferred(NOW), [])

    def test_open_issue_with_past_defer_date_is_ready(self):
        snap = self.snap([issue("t-pd", defer_until=PAST)])
        self.assertEqual(snap.ready(now=NOW), ["t-pd"])

    def test_open_issue_with_future_defer_date_is_hidden_unless_included(self):
        snap = self.snap([issue("t-f", defer_until=FUTURE)])
        self.assertEqual(snap.ready(now=NOW), [])
        self.assertEqual(snap.ready(now=NOW, include_deferred=True), ["t-f"])

    def test_child_of_future_deferred_parent_is_hidden_but_grandchild_is_not(self):
        snap = self.snap([
            issue("t-p", status="deferred", defer_until=FUTURE),
            child("t-p.1", "t-p"),
            child("t-p.1.1", "t-p.1"),
        ])
        self.assertEqual(snap.ready(now=NOW), ["t-p.1.1"])
        self.assertEqual(set(snap.ready(now=NOW, include_deferred=True)), {"t-p.1", "t-p.1.1"})

    def test_child_of_past_deferred_parent_is_ready(self):
        snap = self.snap([issue("t-p", status="deferred", defer_until=PAST),
                          child("t-p.1", "t-p")])
        self.assertEqual(snap.ready(now=NOW), ["t-p.1"])

    def test_a_closed_parent_with_a_future_defer_date_still_hides_its_children(self):
        # measured: bd consults the parent's defer_until, not the parent's status
        snap = self.snap([issue("t-p", status="closed", defer_until=FUTURE),
                          child("t-p.1", "t-p")])
        self.assertEqual(snap.ready(now=NOW), [])
        self.assertEqual(snap.ready(now=NOW, include_deferred=True), ["t-p.1"])

    def test_defer_dates_are_compared_as_times_not_as_text(self):
        # NOW is 2026-09-30T12:00:00Z
        snap = self.snap([
            issue("t-space", defer_until="2026-10-01 00:00:00"),          # space separator
            issue("t-date", defer_until="2026-10-01"),                    # date only
            issue("t-east", defer_until="2026-09-30T13:30:00+02:00"),     # 11:30Z: past
            issue("t-west", defer_until="2026-09-30T08:30:00-05:00"),     # 13:30Z: future
            issue("t-nanos", defer_until="2026-09-30T11:59:59.999999999Z"),
        ])
        self.assertEqual(set(snap.ready(now=NOW)), {"t-east", "t-nanos"})

    def test_order_uses_the_instant_not_the_spelling_of_created_at(self):
        snap = self.snap([
            issue("t-a", created_at="2026-09-10T12:00:00+02:00"),         # 10:00Z
            issue("t-b", created_at="2026-09-10T10:00:01Z"),
            issue("t-c", created_at="2026-09-10 09:59:59"),
        ])
        self.assertEqual(snap.ready(now=NOW), ["t-b", "t-a", "t-c"])

    def test_child_of_closed_parent_is_ready(self):
        snap = self.snap([issue("t-p", status="closed"), child("t-p.1", "t-p")])
        self.assertEqual(snap.ready(now=NOW), ["t-p.1"])

    def test_order_is_priority_then_newest_first_then_id(self):
        snap = self.snap([
            issue("t-low", priority=4, created_at="2026-09-10T10:00:09Z"),
            issue("t-old", priority=1, created_at="2026-09-10T10:00:01Z"),
            issue("t-new", priority=1, created_at="2026-09-10T10:00:02Z"),
            issue("t-tie-b", priority=2, created_at="2026-09-10T10:00:05Z"),
            issue("t-tie-a", priority=2, created_at="2026-09-10T10:00:05Z"),
            issue("t-top", priority=0, created_at="2026-09-10T10:00:00Z"),
        ])
        self.assertEqual(snap.ready(now=NOW),
                         ["t-top", "t-new", "t-old", "t-tie-a", "t-tie-b", "t-low"])

    def test_filters(self):
        snap = self.snap([
            issue("t-e", issue_type="epic", priority=1),
            child("t-e.1", "t-e", issue_type="bug", priority=3, assignee="ann"),
            child("t-e.1.1", "t-e.1", priority=3),
            issue("t-z", priority=3),
        ])
        self.assertEqual(snap.ready(now=NOW, priority=1), ["t-e"])
        self.assertEqual(snap.ready(now=NOW, issue_type="bug"), ["t-e.1"])
        self.assertEqual(set(snap.ready(now=NOW, parent="t-e")), {"t-e.1", "t-e.1.1"})
        self.assertEqual(snap.ready(now=NOW, assignee="ann"), ["t-e.1"])
        self.assertEqual(set(snap.ready(now=NOW, unassigned=True)), {"t-e", "t-e.1.1", "t-z"})


class CountsTest(StoreCase):
    def test_counts_include_only_blocks_edges(self):
        conn, _ = self.build([
            issue("t-p"), issue("t-b"),
            with_deps(issue("t-c"), dep("t-c", "t-p", "parent-child"), dep("t-c", "t-b"),
                      dep("t-c", "t-p", "related")),
        ])
        snap = Snapshot(conn)
        self.assertEqual(snap.issue_object("t-c")["dependency_count"], 1)
        self.assertEqual(snap.issue_object("t-b")["dependent_count"], 1)
        self.assertEqual(snap.issue_object("t-p")["dependent_count"], 0)
        self.assertEqual(snap.issue_object("t-c")["parent"], "t-p")
