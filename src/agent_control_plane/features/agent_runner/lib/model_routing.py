from __future__ import annotations

from dataclasses import dataclass

QUALITY_TIERS = ("mechanical", "balanced", "deep")
_MANAGED_CODEX_MODEL_FAMILIES = frozenset({"luna", "terra", "sol"})
_MANAGED_CODEX_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class ModelProfile:
    model: str
    reasoning_effort: str


def _validated_profile(profile: ModelProfile) -> ModelProfile:
    model = profile.model.strip()
    effort = profile.reasoning_effort.strip().lower()
    if not model:
        raise ValueError("Codex model must not be empty")

    family = model.lower().rsplit("-", maxsplit=1)[-1]
    if family in _MANAGED_CODEX_MODEL_FAMILIES and effort not in _MANAGED_CODEX_REASONING_EFFORTS:
        allowed = ", ".join(_MANAGED_CODEX_REASONING_EFFORTS)
        raise ValueError(
            f"Codex model {model!r} does not support reasoning effort {effort!r}. "
            f"Expected one of: {allowed}"
        )
    if not effort:
        raise ValueError("Codex reasoning effort must not be empty")
    return ModelProfile(model=model, reasoning_effort=effort)


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
        first = _validated_profile(self._profiles[tier])
        deep = _validated_profile(self._profiles["deep"])
        if first == deep:
            return (first,)
        return first, deep

    @staticmethod
    def ladder_for_explicit_model(
        model: str,
        reasoning_effort: str,
    ) -> tuple[ModelProfile, ...]:
        return (_validated_profile(ModelProfile(model, reasoning_effort)),)

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
