from __future__ import annotations

import unittest

from agent_control_plane.entities.workspace import (
    find_forbidden_status_entries,
    find_new_forbidden_status_entries,
)


class GuardrailsTest(unittest.TestCase):
    def test_detects_generated_lockfiles_and_venv_files(self) -> None:
        porcelain = "\n".join(
            [
                " M backend/scripts/init_user.py",
                "?? uv.lock",
                "?? .venv/Scripts/python.exe",
                "?? backend/tests/test_example.py",
            ]
        )

        entries = find_forbidden_status_entries(
            porcelain,
            ("uv.lock", ".venv/**"),
        )

        self.assertEqual([entry.path for entry in entries], ["uv.lock", ".venv/Scripts/python.exe"])

    def test_detects_renamed_lockfile_target(self) -> None:
        entries = find_forbidden_status_entries(
            "R  old.lock -> uv.lock",
            ("uv.lock",),
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "uv.lock")

    def test_ignores_forbidden_entries_present_before_job_start(self) -> None:
        baseline = find_forbidden_status_entries(" M uv.lock", ("uv.lock", ".venv/**"))

        entries = find_new_forbidden_status_entries(
            " M uv.lock\n?? .venv/Scripts/python.exe",
            ("uv.lock", ".venv/**"),
            baseline,
        )

        self.assertEqual([entry.path for entry in entries], [".venv/Scripts/python.exe"])

    def test_detects_status_change_for_preexisting_forbidden_entry(self) -> None:
        baseline = find_forbidden_status_entries(" M uv.lock", ("uv.lock",))

        entries = find_new_forbidden_status_entries("MM uv.lock", ("uv.lock",), baseline)

        self.assertEqual([entry.path for entry in entries], ["uv.lock"])


if __name__ == "__main__":
    unittest.main()
