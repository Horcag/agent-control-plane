from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from agent_control_plane.features.agent_runner.lib.model_catalog import ModelCatalog

LEGACY_POLICY_NAMES = ("mechanical", "balanced", "deep")
_TERMINAL_RESULT_STATUSES = frozenset({"completed", "partial", "blocked", "failed"})
_AUTOMATIC_SELECTION_SOURCES = frozenset({"configured_fallback", "history"})


@dataclass(frozen=True)
class ModelProfile:
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class AdaptiveRoutingSettings:
    """Conservative statistical guardrails for one configured routing policy."""

    minimum_samples_per_candidate: int
    history_window: int
    quality_floor: float
    prior_quality: float
    prior_weight: float
    allow_missing_price: bool = False

    def __post_init__(self) -> None:
        if self.minimum_samples_per_candidate <= 0:
            raise ValueError("minimum_samples_per_candidate must be positive")
        if self.history_window <= 0:
            raise ValueError("history_window must be positive")
        if not 0.0 <= self.quality_floor <= 1.0:
            raise ValueError("quality_floor must be between 0 and 1")
        if not 0.0 <= self.prior_quality <= 1.0:
            raise ValueError("prior_quality must be between 0 and 1")
        if self.prior_weight <= 0:
            raise ValueError("prior_weight must be positive")


@dataclass(frozen=True)
class RoutingPolicy:
    """One named ordered ladder, optionally eligible for conservative adaptation."""

    name: str
    task_class: str
    tool_call_budget: int
    candidates: tuple[ModelProfile, ...]
    adaptive: AdaptiveRoutingSettings | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Routing policy name must not be empty")
        if not self.task_class.strip():
            raise ValueError("Routing policy task_class must not be empty")
        if self.tool_call_budget <= 0:
            raise ValueError("Routing policy tool_call_budget must be positive")
        if not self.candidates:
            raise ValueError("Routing policy needs at least one candidate")
        normalized = [
            (candidate.model.strip().lower(), candidate.reasoning_effort.strip().lower())
            for candidate in self.candidates
        ]
        if any(not model or not effort for model, effort in normalized):
            raise ValueError("Routing policy candidates require model and reasoning effort")
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"Routing policy has duplicate candidates: {self.name}")


@dataclass(frozen=True)
class RoutingHistoryRecord:
    """Comparable persisted attempt facts, supplied from JobStore rather than a new database."""

    model: str
    reasoning_effort: str
    attempt_status: str
    result_status: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    duration_sec: float
    root_outcome: str | None
    defects_found: int
    catalog_source: str | None
    catalog_version: str | None = None
    metrics_valid: bool = True
    route: str | None = None
    policy_name: str | None = None
    task_class: str | None = None
    selection_source: str | None = None


@dataclass(frozen=True)
class CandidateScore:
    model: str
    reasoning_effort: str
    configured_index: int
    sample_count: int
    success_count: int
    review_penalty_count: int
    quality_score: float | None
    expected_api_usd: float | None
    expected_duration_sec: float | None
    eligible: bool
    exclusion_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "configured_index": self.configured_index,
            "sample_count": self.sample_count,
            "success_count": self.success_count,
            "review_penalty_count": self.review_penalty_count,
            "quality_score": self.quality_score,
            "expected_api_usd": self.expected_api_usd,
            "expected_duration_sec": self.expected_duration_sec,
            "eligible": self.eligible,
            "exclusion_reasons": list(self.exclusion_reasons),
        }


@dataclass(frozen=True)
class RoutingDecision:
    requested_policy: str
    task_class: str
    tool_call_budget: int
    chosen_profile: ModelProfile
    ladder: tuple[ModelProfile, ...]
    selection_source: str
    catalog_source: str
    catalog_version: str | None
    candidate_scores: tuple[CandidateScore, ...]
    excluded_data_reasons: tuple[str, ...]

    @property
    def configured_fallback(self) -> bool:
        return self.selection_source == "configured_fallback"

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_policy": self.requested_policy,
            "task_class": self.task_class,
            "tool_call_budget": self.tool_call_budget,
            "chosen_profile": _profile_payload(self.chosen_profile),
            "ladder": [_profile_payload(profile) for profile in self.ladder],
            "catalog": {
                "source": self.catalog_source,
                "version": self.catalog_version,
            },
            "candidate_scores": [score.as_dict() for score in self.candidate_scores],
            "excluded_data_reasons": list(self.excluded_data_reasons),
            "selection_source": self.selection_source,
            "configured_fallback": self.configured_fallback,
        }


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
    """Catalog-backed named policy ladders with a conservative adaptive first choice."""

    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        policies: tuple[RoutingPolicy, ...] | None = None,
        mechanical: ModelProfile | None = None,
        balanced: ModelProfile | None = None,
        deep: ModelProfile | None = None,
        mechanical_tool_call_budget: int = 45,
        balanced_tool_call_budget: int = 80,
        deep_tool_call_budget: int = 120,
    ) -> None:
        self._catalog = catalog
        definitions = policies or _legacy_policy_definitions(
            mechanical,
            balanced,
            deep,
            mechanical_tool_call_budget=mechanical_tool_call_budget,
            balanced_tool_call_budget=balanced_tool_call_budget,
            deep_tool_call_budget=deep_tool_call_budget,
        )
        normalized: dict[str, RoutingPolicy] = {}
        for definition in definitions:
            key = definition.name.strip().lower()
            if key in normalized:
                raise ValueError(f"Duplicate routing policy: {definition.name}")
            normalized[key] = definition
        self._policies = normalized

    @property
    def catalog(self) -> ModelCatalog:
        return self._catalog

    @property
    def policy_names(self) -> tuple[str, ...]:
        return tuple(policy.name for policy in self._policies.values())

    def policy(self, policy_name: str) -> RoutingPolicy:
        key = policy_name.strip().lower()
        try:
            return self._policies[key]
        except KeyError as exc:
            configured = ", ".join(self.policy_names) or "none"
            raise ValueError(
                f"Unsupported Codex routing policy {policy_name!r}. Configured policies: {configured}"
            ) from exc

    def validate_configured_candidates(self) -> None:
        """Fail early for loaded inventories while retaining missing-cache diagnostics."""
        if self.catalog.cache_status != "loaded":
            return
        for policy in self._policies.values():
            self._resolved_candidates(policy)

    def ladder_for_policy(self, policy_name: str) -> tuple[ModelProfile, ...]:
        return self.decision_for_policy(policy_name, history=()).ladder

    def ladder_for_tier(self, quality_tier: str) -> tuple[ModelProfile, ...]:
        """Compatibility adapter for legacy mechanical/balanced/deep callers."""
        if quality_tier.strip().lower() not in self._policies:
            raise ValueError(f"Unsupported Codex quality tier {quality_tier!r}")
        return self.ladder_for_policy(quality_tier)

    def tool_call_budget_for_policy(self, policy_name: str) -> int:
        return self.policy(policy_name).tool_call_budget

    def decision_for_policy(
        self,
        policy_name: str,
        *,
        history: Iterable[RoutingHistoryRecord],
        route: str | None = None,
    ) -> RoutingDecision:
        policy = self.policy(policy_name)
        candidates = self._resolved_candidates(policy)
        records = tuple(history)
        if policy.adaptive is None:
            return self._configured_decision(
                policy,
                candidates,
                records,
                excluded_data_reasons=("adaptive routing is disabled for this policy",),
            )
        comparable, excluded = self._comparable_history(
            records,
            policy=policy,
            route=route,
            history_window=policy.adaptive.history_window,
        )
        scores = tuple(
            self._score_candidate(
                candidate,
                configured_index=index,
                records=comparable,
                settings=policy.adaptive,
            )
            for index, candidate in enumerate(candidates)
        )
        eligible = tuple(score for score in scores if score.eligible)
        if not eligible:
            reasons = tuple(dict.fromkeys((*excluded, "insufficient comparable history")))
            return self._configured_decision(
                policy,
                candidates,
                records,
                candidate_scores=scores,
                excluded_data_reasons=reasons,
            )
        best = min(
            eligible,
            key=lambda score: (
                -(score.quality_score or 0.0),
                score.expected_api_usd is None,
                score.expected_api_usd if score.expected_api_usd is not None else float("inf"),
                score.expected_duration_sec is None,
                (
                    score.expected_duration_sec
                    if score.expected_duration_sec is not None
                    else float("inf")
                ),
                score.configured_index,
            ),
        )
        chosen = candidates[best.configured_index]
        ladder = (chosen, *(candidate for candidate in candidates if candidate != chosen))
        return RoutingDecision(
            requested_policy=policy.name,
            task_class=policy.task_class,
            tool_call_budget=policy.tool_call_budget,
            chosen_profile=chosen,
            ladder=ladder,
            selection_source="history",
            catalog_source=self.catalog.source,
            catalog_version=self.catalog.version,
            candidate_scores=scores,
            excluded_data_reasons=tuple(dict.fromkeys(excluded)),
        )

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

    def _resolved_candidates(self, policy: RoutingPolicy) -> tuple[ModelProfile, ...]:
        return tuple(
            _validated_profile(candidate, catalog=self.catalog, automatic=True)
            for candidate in policy.candidates
        )

    def _configured_decision(
        self,
        policy: RoutingPolicy,
        candidates: tuple[ModelProfile, ...],
        records: tuple[RoutingHistoryRecord, ...],
        *,
        candidate_scores: tuple[CandidateScore, ...] | None = None,
        excluded_data_reasons: tuple[str, ...],
    ) -> RoutingDecision:
        scores = candidate_scores or tuple(
            CandidateScore(
                model=candidate.model,
                reasoning_effort=candidate.reasoning_effort,
                configured_index=index,
                sample_count=0,
                success_count=0,
                review_penalty_count=0,
                quality_score=None,
                expected_api_usd=None,
                expected_duration_sec=None,
                eligible=False,
                exclusion_reasons=("adaptive routing is disabled for this policy",),
            )
            for index, candidate in enumerate(candidates)
        )
        del records
        return RoutingDecision(
            requested_policy=policy.name,
            task_class=policy.task_class,
            tool_call_budget=policy.tool_call_budget,
            chosen_profile=candidates[0],
            ladder=candidates,
            selection_source="configured_fallback",
            catalog_source=self.catalog.source,
            catalog_version=self.catalog.version,
            candidate_scores=scores,
            excluded_data_reasons=excluded_data_reasons,
        )

    def _comparable_history(
        self,
        records: tuple[RoutingHistoryRecord, ...],
        *,
        policy: RoutingPolicy,
        route: str | None,
        history_window: int,
    ) -> tuple[tuple[RoutingHistoryRecord, ...], tuple[str, ...]]:
        comparable: list[RoutingHistoryRecord] = []
        excluded: list[str] = []
        for record in records:
            if not record.metrics_valid:
                excluded.append("invalid attempt metrics")
                continue
            if record.result_status not in _TERMINAL_RESULT_STATUSES:
                excluded.append("missing terminal result")
                continue
            if not _present(record.route) or not _present(route):
                excluded.append("missing route")
                continue
            if record.route != route:
                excluded.append("unrelated route")
                continue
            if not _present(record.policy_name):
                excluded.append("missing policy")
                continue
            if record.policy_name != policy.name:
                excluded.append("unrelated policy")
                continue
            if not _present(record.task_class):
                excluded.append("missing task class")
                continue
            if record.task_class != policy.task_class:
                excluded.append("unrelated task class")
                continue
            if not _present(record.selection_source):
                excluded.append("missing selection source")
                continue
            if record.selection_source not in _AUTOMATIC_SELECTION_SOURCES:
                excluded.append("explicit selection")
                continue
            if not _present(record.catalog_source) or not _present(self.catalog.source):
                excluded.append("missing catalog source")
                continue
            if record.catalog_source != self.catalog.source:
                excluded.append("incompatible catalog source")
                continue
            if not _present(record.catalog_version) or not _present(self.catalog.version):
                excluded.append("missing catalog version")
                continue
            if record.catalog_version != self.catalog.version:
                excluded.append("incompatible catalog version")
                continue
            try:
                usage = tuple(
                    float(value)
                    for value in (
                        record.input_tokens,
                        record.cached_input_tokens,
                        record.output_tokens,
                        record.duration_sec,
                    )
                )
            except (TypeError, ValueError):
                usage = ()
            if (
                len(usage) != 4
                or any(not math.isfinite(value) or value < 0 for value in usage)
                or usage[1] > usage[0]
            ):
                excluded.append("invalid raw usage")
                continue
            comparable.append(record)
            if len(comparable) >= history_window:
                break
        return tuple(comparable), tuple(dict.fromkeys(excluded))

    def _score_candidate(
        self,
        candidate: ModelProfile,
        *,
        configured_index: int,
        records: tuple[RoutingHistoryRecord, ...],
        settings: AdaptiveRoutingSettings,
    ) -> CandidateScore:
        matches = tuple(
            record
            for record in records
            if record.model.strip().lower() == candidate.model.strip().lower()
            and record.reasoning_effort.strip().lower() == candidate.reasoning_effort
        )
        successes = sum(_quality_success(record) for record in matches)
        review_penalties = sum(_has_review_penalty(record) for record in matches)
        quality_score = (
            (successes + settings.prior_quality * settings.prior_weight)
            / (len(matches) + settings.prior_weight)
            if matches
            else None
        )
        prices = [
            self.catalog.reprice(
                candidate.model,
                input_tokens=record.input_tokens,
                cached_input_tokens=record.cached_input_tokens,
                output_tokens=record.output_tokens,
            ).estimated_api_usd
            for record in matches
        ]
        priced = [price for price in prices if price is not None]
        expected_api_usd = (
            sum(priced) / len(priced) if len(priced) == len(matches) and priced else None
        )
        durations = [record.duration_sec for record in matches]
        expected_duration = sum(durations) / len(durations) if durations else None
        reasons: list[str] = []
        if len(matches) < settings.minimum_samples_per_candidate:
            reasons.append("insufficient comparable samples")
        if any(record.root_outcome is None for record in matches):
            reasons.append("missing root review")
        if review_penalties:
            reasons.append("root rejection or defect")
        if quality_score is not None and quality_score < settings.quality_floor:
            reasons.append("quality floor not met")
        if matches and expected_api_usd is None and not settings.allow_missing_price:
            reasons.append("missing current price")
        eligible = not reasons
        return CandidateScore(
            model=candidate.model,
            reasoning_effort=candidate.reasoning_effort,
            configured_index=configured_index,
            sample_count=len(matches),
            success_count=successes,
            review_penalty_count=review_penalties,
            quality_score=quality_score,
            expected_api_usd=expected_api_usd,
            expected_duration_sec=expected_duration,
            eligible=eligible,
            exclusion_reasons=tuple(reasons),
        )


def _legacy_policy_definitions(
    mechanical: ModelProfile | None,
    balanced: ModelProfile | None,
    deep: ModelProfile | None,
    *,
    mechanical_tool_call_budget: int,
    balanced_tool_call_budget: int,
    deep_tool_call_budget: int,
) -> tuple[RoutingPolicy, ...]:
    """Adapt the pre-named-policy mechanical/balanced/deep configuration."""
    if mechanical is None or balanced is None or deep is None:
        raise ValueError(
            "Named policies or all legacy mechanical/balanced/deep profiles are required"
        )
    return (
        RoutingPolicy(
            "mechanical",
            "mechanical",
            mechanical_tool_call_budget,
            _legacy_candidates(mechanical, deep),
        ),
        RoutingPolicy(
            "balanced",
            "balanced",
            balanced_tool_call_budget,
            _legacy_candidates(balanced, deep),
        ),
        RoutingPolicy("deep", "deep", deep_tool_call_budget, (deep,)),
    )


def _legacy_candidates(primary: ModelProfile, fallback: ModelProfile) -> tuple[ModelProfile, ...]:
    return (primary,) if primary == fallback else (primary, fallback)


def _quality_success(record: RoutingHistoryRecord) -> bool:
    return (
        record.result_status == "completed"
        and record.root_outcome == "accepted"
        and not _has_review_penalty(record)
    )


def _has_review_penalty(record: RoutingHistoryRecord) -> bool:
    return record.root_outcome == "rejected" or record.defects_found > 0


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _profile_payload(profile: ModelProfile) -> dict[str, str]:
    return {"model": profile.model, "reasoning_effort": profile.reasoning_effort}
