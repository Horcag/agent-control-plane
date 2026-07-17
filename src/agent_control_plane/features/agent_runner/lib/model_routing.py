from __future__ import annotations

from dataclasses import dataclass

from agent_control_plane.features.agent_runner.lib.model_catalog import ModelCatalog

QUALITY_TIERS = ("mechanical", "balanced", "deep")


@dataclass(frozen=True)
class ModelProfile:
    model: str
    reasoning_effort: str


def _validated_profile(
    profile: ModelProfile,
    *,
    catalog: ModelCatalog,
    automatic: bool,
) -> ModelProfile:
    model = profile.model.strip()
    effort = profile.reasoning_effort.strip().lower()
    if not model:
        raise ValueError("Codex model must not be empty")
    if not effort:
        raise ValueError("Codex reasoning effort must not be empty")
    if automatic or model.lower() == "default":
        model = catalog.resolve_automatic_profile(model, effort)
    else:
        catalog.validate_explicit_profile(model, effort)
    return ModelProfile(model=model, reasoning_effort=effort)


class ModelRoutingPolicy:
    """Validate catalog-backed quality profiles and their deterministic fallback ladder."""

    def __init__(
        self,
        *,
        mechanical: ModelProfile,
        balanced: ModelProfile,
        deep: ModelProfile,
        catalog: ModelCatalog,
    ) -> None:
        self._profiles = {
            "mechanical": mechanical,
            "balanced": balanced,
            "deep": deep,
        }
        self._catalog = catalog

    @property
    def catalog(self) -> ModelCatalog:
        return self._catalog

    def ladder_for_tier(self, quality_tier: str) -> tuple[ModelProfile, ...]:
        tier = quality_tier.strip().lower()
        if tier not in self._profiles:
            allowed = ", ".join(QUALITY_TIERS)
            raise ValueError(
                f"Unsupported quality tier {quality_tier!r}. Expected one of: {allowed}"
            )
        first = _validated_profile(self._profiles[tier], catalog=self._catalog, automatic=True)
        deep = _validated_profile(self._profiles["deep"], catalog=self._catalog, automatic=True)
        if first == deep:
            return (first,)
        return first, deep

    def ladder_for_explicit_model(
        self,
        model: str,
        reasoning_effort: str,
    ) -> tuple[ModelProfile, ...]:
        return (
            _validated_profile(
                ModelProfile(model, reasoning_effort),
                catalog=self._catalog,
                automatic=False,
            ),
        )

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
