from __future__ import annotations

from agent_control_plane.features.result_handoff import parse_result_report


def test_result_report_parser_separates_claims_and_flags_missing_sections() -> None:
    complete = parse_result_report(
        """Status: completed

Changed files:
- src/app.py
- tests/test_app.py

What changed:
- Added the bounded behavior.

Verification performed:
- pytest -q tests/test_app.py (passed)
- ruff check src/app.py (passed)

Not verified / remaining risks:
- Sonar was unavailable.
"""
    )
    incomplete = parse_result_report("Status: completed\nChanged files: none\n")

    assert complete["format_valid"] is True
    assert complete["missing_sections"] == []
    assert complete["changed_files_claimed"] == ["src/app.py", "tests/test_app.py"]
    assert complete["verification_claims"] == [
        "pytest -q tests/test_app.py (passed)",
        "ruff check src/app.py (passed)",
    ]
    assert complete["claims_trust"] == "worker_reported"
    assert incomplete["format_valid"] is False
    assert incomplete["missing_sections"] == [
        "what_changed",
        "verification_performed",
        "remaining_risks",
    ]


def test_result_report_parser_normalizes_generated_git_status_file_list() -> None:
    report = parse_result_report(
        """Status: blocked

Changed files: M src/app.py; ?? tests/test_app.py

What changed: preserved

Verification performed: none

Not verified / remaining risks: interrupted
"""
    )

    assert report["changed_files_claimed"] == ["src/app.py", "tests/test_app.py"]
