from __future__ import annotations

import re

# Canonical section keys in the order `result.md` is expected to present them, mapped to
# their exact expected heading label. Both the worker-facing result_handoff validator
# (features/result_handoff/lib/verification_bundle.py) and the agent_runner process
# monitors need this same notion of "is the envelope well-formed" — one to score a
# finished run, the other to decide whether a still-running worker deserves a grace
# window to fix it before being terminated. Feature slices cannot import each other, so
# the shared section pattern lives here to keep both checks from drifting apart.
RESULT_ENVELOPE_SECTIONS: dict[str, str] = {
    "changed_files": "Changed files",
    "what_changed": "What changed",
    "verification_performed": "Verification performed",
    "remaining_risks": "Not verified / remaining risks",
}

RESULT_ENVELOPE_SECTION_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:#{1,6}\s*)?(?:\*\*)?"
    r"(Changed files|What changed|Verification performed|"
    r"Not verified\s*/\s*remaining risks)(?:\*\*)?\s*:?[ \t]*(.*)$",
    re.IGNORECASE,
)


def result_envelope_section_key(label: str) -> str:
    normalized = " ".join(label.lower().split())
    if normalized.startswith("changed files"):
        return "changed_files"
    if normalized.startswith("what changed"):
        return "what_changed"
    if normalized.startswith("verification performed"):
        return "verification_performed"
    return "remaining_risks"


def missing_result_envelope_sections(text: str) -> tuple[str, ...]:
    """Return the canonical section keys absent from a result.md envelope's text."""
    seen: set[str] = set()
    for raw_line in text.splitlines():
        match = RESULT_ENVELOPE_SECTION_PATTERN.match(raw_line)
        if match:
            seen.add(result_envelope_section_key(match.group(1)))
    return tuple(name for name in RESULT_ENVELOPE_SECTIONS if name not in seen)


def result_envelope_expected_headings() -> tuple[str, ...]:
    return tuple(f"## {label}" for label in RESULT_ENVELOPE_SECTIONS.values())


def result_envelope_missing_headings(missing_sections: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"## {RESULT_ENVELOPE_SECTIONS[name]}" for name in missing_sections)
