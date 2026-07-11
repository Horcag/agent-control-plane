from __future__ import annotations

from dataclasses import dataclass

QUALITY_TIERS = ("mechanical", "balanced", "deep")


@dataclass(frozen=True)
class ModelProfile:
    model: str
    reasoning_effort: str


class ModelRoutingPolicy:
    """Conservative model ladder: Luna is opt-in, Terra is the quality fallback."""

    def __init__(
        self,
        *,
        mechanical: ModelProfile,
        balanced: ModelProfile,
        deep: ModelProfile,
    ) -> None:
        self._profiles = {
            "mechanical": mechanical,
            "balanced": balanced,
            "deep": deep,
        }

    def ladder_for_tier(self, quality_tier: str) -> tuple[ModelProfile, ...]:
        tier = quality_tier.strip().lower()
        if tier not in self._profiles:
            allowed = ", ".join(QUALITY_TIERS)
            raise ValueError(
                f"Unsupported quality tier {quality_tier!r}. Expected one of: {allowed}"
            )
        first = self._profiles[tier]
        deep = self._profiles["deep"]
        if first == deep:
            return (first,)
        return first, deep

    @staticmethod
    def ladder_for_explicit_model(
        model: str,
        reasoning_effort: str,
    ) -> tuple[ModelProfile, ...]:
        return (ModelProfile(model, reasoning_effort),)

    @staticmethod
    def should_escalate(
        *,
        runner_status: str,
        result_status: str | None,
        has_next: bool,
    ) -> bool:
        if not has_next:
            return False
        if result_status in {"partial", "blocked"}:
            return True
        return runner_status in {
            "capacity",
            "exited_without_result",
            "timeout",
            "idle_timeout",
            "no_progress_timeout",
            "tool_timeout",
        }
