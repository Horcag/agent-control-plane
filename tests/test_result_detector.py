from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result


class ResultDetectorTest(unittest.TestCase):
    def test_detects_colon_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.md"
            started_at = 0.0
            result.write_text("Status: completed\n", encoding="utf-8")

            state = inspect_result(result, started_at)

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
