from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.result_detector import (
    contains_capacity_marker,
    inspect_result,
    recover_result_from_last_message,
)


class ResultDetectorTest(unittest.TestCase):
    def test_detects_colon_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.md"
            started_at = 0.0
            result.write_text("Status: completed\n", encoding="utf-8")

            state = inspect_result(result, started_at)

            self.assertTrue(state.done)
            self.assertEqual(state.status, "completed")
            self.assertEqual(state.verification_state, "missing")

    def test_verification_bundle_is_validated_without_controlling_termination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = root / "result.md"
            result.write_text("Status: completed\n", encoding="utf-8")
            (root / "verification.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "completed",
                        "changed_files": [{"path": "src/app.py", "change": "modified"}],
                        "checks": [
                            {
                                "command": "pytest -q",
                                "cwd": ".",
                                "outcome": "passed",
                                "exit_code": 0,
                                "summary": "3 passed",
                            }
                        ],
                        "unverified": [],
                    }
                ),
                encoding="utf-8",
            )

            state = inspect_result(result, started_at=0.0)

            self.assertTrue(state.done)
            self.assertEqual(state.verification_state, "valid")
            self.assertIsNone(state.verification_error)

    def test_invalid_or_status_mismatched_bundle_still_terminates_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = root / "result.md"
            result.write_text("Status: completed\n", encoding="utf-8")
            verification = root / "verification.json"
            verification.write_text("{broken", encoding="utf-8")

            malformed = inspect_result(result, started_at=0.0)
            verification.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "blocked",
                        "changed_files": [],
                        "checks": [],
                        "unverified": [],
                    }
                ),
                encoding="utf-8",
            )
            mismatched = inspect_result(result, started_at=0.0)

            self.assertTrue(malformed.done)
            self.assertEqual(malformed.verification_state, "invalid")
            self.assertTrue(mismatched.done)
            self.assertEqual(mismatched.verification_state, "invalid")
            self.assertIn("status", mismatched.verification_error or "")

    def test_detects_inline_code_status_without_matching_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.md"
            result.write_text(
                "## Status\n`Status: partial`\n\nThe prose mentions Status: completed later.\n",
                encoding="utf-8",
            )

            state = inspect_result(result, started_at=0.0)

            self.assertTrue(state.done)
            self.assertEqual(state.status, "partial")

    def test_normalizes_agy_success_status_to_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.md"
            result.write_text("Status: success\n", encoding="utf-8")

            state = inspect_result(result, started_at=0.0)

            self.assertTrue(state.done)
            self.assertEqual(state.status, "completed")

    def test_detects_markdown_heading_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.md"
            started_at = 0.0
            result.write_text("## Status\nblocked\n", encoding="utf-8")

            state = inspect_result(result, started_at)

            self.assertTrue(state.done)
            self.assertEqual(state.status, "blocked")

    def test_detects_bold_list_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.md"
            started_at = 0.0
            result.write_text("- **Status**: completed\n", encoding="utf-8")

            state = inspect_result(result, started_at)

            self.assertTrue(state.done)
            self.assertEqual(state.status, "completed")

    def test_recovers_result_when_codex_only_wrote_last_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = root / "result.md"
            last_message = root / "attempt-001.last-message.md"
            last_message.write_text(
                "Status: completed\n\nChanged files: backend/app.py\n",
                encoding="utf-8",
            )

            state = recover_result_from_last_message(result, last_message, started_at=0.0)

            self.assertTrue(state.done)
            self.assertEqual(state.status, "completed")
            self.assertEqual(
                result.read_text(encoding="utf-8"), last_message.read_text(encoding="utf-8")
            )

    def test_does_not_recover_last_message_without_status_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = root / "result.md"
            last_message = root / "attempt-001.last-message.md"
            last_message.write_text("I ran out of capacity", encoding="utf-8")

            state = recover_result_from_last_message(result, last_message, started_at=0.0)

            self.assertFalse(state.done)
            self.assertFalse(result.exists())

    def test_detects_codex_usage_limit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "attempt.log"
            log.write_text("You've hit your usage limit. Try again later.", encoding="utf-8")

            self.assertTrue(contains_capacity_marker(log))

    def test_placeholder_is_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.md"
            started_at = 0.0
            result.write_text("Awaiting `agy`\nStatus: blocked\n", encoding="utf-8")

            state = inspect_result(result, started_at)

            self.assertFalse(state.done)
            self.assertIn("placeholder", state.reason or "")


if __name__ == "__main__":
    unittest.main()
