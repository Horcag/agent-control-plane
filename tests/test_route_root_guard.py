from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_control_plane.app.runtime.orchestrator import (
    AgentControlPlane,
    WorkspaceDirtyBaseline,
)
from agent_control_plane.features.slot_lifecycle.lib.route_root_guard import (
    RouteRootGuard,
    RouteRootSnapshot,
)


class RouteRootGuardTest(unittest.TestCase):
    def test_preexisting_untracked_entry_is_part_of_the_baseline(self) -> None:
        entries = {"identifier.sqlite": ("??", "file:original")}
        guard = RouteRootGuard(head="head-a", entries=entries)

        changed = guard.evaluate(
            _snapshot(head="head-a", entries=entries),
            now=10.0,
            staged_grace_sec=5.0,
        )

        self.assertEqual(changed, ())

    def test_new_worktree_or_untracked_change_is_rejected_immediately(self) -> None:
        guard = RouteRootGuard(head="head-a", entries={})

        tracked = guard.evaluate(
            _snapshot(
                head="head-a",
                entries={"tracked.py": (" M", "file:changed")},
            ),
            now=10.0,
            staged_grace_sec=5.0,
        )
        untracked = guard.evaluate(
            _snapshot(
                head="head-a",
                entries={"unexpected.py": ("??", "file:new")},
            ),
            now=10.0,
            staged_grace_sec=5.0,
        )

        self.assertEqual(tracked, ("tracked.py",))
        self.assertEqual(untracked, ("unexpected.py",))

    def test_staged_then_committed_external_change_is_accepted(self) -> None:
        guard = RouteRootGuard(head="head-a", entries={})

        staged = guard.evaluate(
            _snapshot(
                head="head-a",
                entries={"committed.py": ("A ", "file:new")},
            ),
            now=10.0,
            staged_grace_sec=5.0,
        )
        committed = guard.evaluate(
            _snapshot(head="head-b", entries={}),
            now=11.0,
            staged_grace_sec=5.0,
        )

        self.assertEqual(staged, ())
        self.assertEqual(committed, ())
        self.assertEqual(guard.head, "head-b")
        self.assertIsNone(guard.pending_index_since)

    def test_staged_change_without_commit_is_rejected_after_grace(self) -> None:
        guard = RouteRootGuard(head="head-a", entries={})
        staged = _snapshot(
            head="head-a",
            entries={"staged.py": ("A ", "file:new")},
        )

        first = guard.evaluate(staged, now=10.0, staged_grace_sec=5.0)
        expired = guard.evaluate(staged, now=15.0, staged_grace_sec=5.0)

        self.assertEqual(first, ())
        self.assertEqual(expired, ("staged.py",))

    def test_head_only_external_commit_is_accepted(self) -> None:
        guard = RouteRootGuard(head="head-a", entries={})

        changed = guard.evaluate(
            _snapshot(head="head-b", entries={}),
            now=10.0,
            staged_grace_sec=5.0,
        )

        self.assertEqual(changed, ())
        self.assertEqual(guard.head, "head-b")

    def test_head_advance_with_remaining_dirty_change_is_rejected(self) -> None:
        guard = RouteRootGuard(head="head-a", entries={})

        changed = guard.evaluate(
            _snapshot(
                head="head-b",
                entries={"leftover.py": (" M", "file:dirty")},
            ),
            now=10.0,
            staged_grace_sec=5.0,
        )

        self.assertEqual(changed, ("leftover.py",))
        self.assertEqual(guard.head, "head-a")

    def test_unstable_snapshot_is_deferred_without_mutating_state(self) -> None:
        guard = RouteRootGuard(head="head-a", entries={})

        changed = guard.evaluate(
            _snapshot(
                head="head-b",
                entries={"racing.py": ("A ", "file:new")},
                stable=False,
            ),
            now=10.0,
            staged_grace_sec=5.0,
        )

        self.assertEqual(changed, ())
        self.assertEqual(guard.head, "head-a")
        self.assertIsNone(guard.pending_index_since)

    def test_real_staged_then_commit_transition_does_not_stop_slot_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_root = root / "repo"
            route_root.mkdir()
            _run(["git", "init"], route_root)
            tracked = route_root / "tracked.py"
            tracked.write_text("before\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], route_root)
            _commit(route_root, "seed")
            (route_root / "identifier.sqlite").write_text("keep\n", encoding="utf-8")

            control = AgentControlPlane.__new__(AgentControlPlane)
            initial = control._route_root_snapshot(route_root)
            baseline = WorkspaceDirtyBaseline(
                path=route_root,
                guard=RouteRootGuard(
                    head=initial.head,
                    entries=dict(initial.entries),
                ),
            )
            job = Mock()
            job.workspace_path = root / "slot"
            job.run_dir = root / "run"
            job.run_dir.mkdir()

            tracked.write_text("after\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], route_root)
            with patch(
                "agent_control_plane.app.runtime.orchestrator.time.monotonic",
                return_value=10.0,
            ):
                staged_message = control._route_root_guardrail_message(job, baseline)

            _commit(route_root, "integrate")
            with patch(
                "agent_control_plane.app.runtime.orchestrator.time.monotonic",
                return_value=11.0,
            ):
                committed_message = control._route_root_guardrail_message(job, baseline)

            self.assertIsNone(staged_message)
            self.assertIsNone(committed_message)
            self.assertNotEqual(baseline.guard.head, initial.head)


def _snapshot(
    *,
    head: str,
    entries: dict[str, tuple[str, str]],
    stable: bool = True,
) -> RouteRootSnapshot:
    return RouteRootSnapshot(
        head=head,
        entries=entries,
        porcelain="",
        stable=stable,
    )


def _commit(repo: Path, message: str) -> None:
    _run(
        [
            "git",
            "-c",
            "user.name=ACP Test",
            "-c",
            "user.email=acp@example.test",
            "commit",
            "-m",
            message,
        ],
        repo,
    )


def _run(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git is not installed") from exc


if __name__ == "__main__":
    unittest.main()
