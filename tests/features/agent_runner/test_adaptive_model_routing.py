from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner import (
    AdaptiveRoutingSettings,
    ModelProfile,
    ModelRoutingPolicy,
    RoutingHistoryRecord,
    RoutingPolicy,
    parse_routing_history_record,
    parse_routing_history_records,
)
from agent_control_plane.features.agent_runner.lib.model_catalog import (
    CatalogModelMetadata,
    CatalogRate,
    ModelCatalog,
)


def _catalog(*models: str, unpriced: tuple[str, ...] = ()) -> ModelCatalog:
    with tempfile.TemporaryDirectory() as temp:
        cache_path = Path(temp) / "models_cache.json"
        cache_path.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": model,
                            "supported_reasoning_levels": ["low", "medium", "ultra"],
                        }
                        for model in models
                    ]
                }
            ),
            encoding="utf-8",
        )
        metadata = tuple(
            CatalogModelMetadata(
                model=model,
                credit_rate=(
                    None
                    if model in unpriced
                    else CatalogRate(input=1.0, cached_input=0.1, output=2.0)
                ),
                api_usd_rate=(
                    None
                    if model in unpriced
                    else CatalogRate(input=1.0, cached_input=0.1, output=2.0)
                ),
                rate_card_version=(None if model in unpriced else "test-rate-card"),
                rate_card_source=(None if model in unpriced else "test"),
            )
            for model in models
        )
        return ModelCatalog.load(
            cache_path=cache_path,
            max_cache_age_sec=60.0,
            metadata=metadata,
        )


def _history(
    model: str,
    *,
    result_status: str = "completed",
    root_outcome: str | None = "accepted",
    defects_found: int = 0,
    duration_sec: float = 30.0,
    route: str | None = "main",
    policy_name: str | None = "code-change",
    task_class: str | None = "implementation",
    selection_source: str | None = "configured_fallback",
    catalog_version: str | None = None,
    metrics_valid: bool = True,
) -> RoutingHistoryRecord:
    return RoutingHistoryRecord(
        model=model,
        reasoning_effort="medium",
        attempt_status="completed",
        result_status=result_status,
        input_tokens=1_000,
        cached_input_tokens=0,
        output_tokens=100,
        duration_sec=duration_sec,
        root_outcome=root_outcome,
        defects_found=defects_found,
        catalog_source="models_cache.json",
        catalog_version=catalog_version,
        metrics_valid=metrics_valid,
        route=route,
        policy_name=policy_name,
        task_class=task_class,
        selection_source=selection_source,
    )


def _history_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "model": "reliable-model",
        "reasoning_effort": "medium",
        "attempt_status": "completed",
        "result_status": "completed",
        "input_tokens": 1_000,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "duration_sec": 30.0,
        "root_outcome": "accepted",
        "defects_found": 0,
        "catalog_source": "models_cache.json",
        "catalog_version": "test-version",
        "metrics_valid": True,
        "route": "main",
        "policy_name": "code-change",
        "task_class": "implementation",
        "selection_source": "configured_fallback",
    }
    row.update(overrides)
    return row


def _adaptive_routing(
    *, minimum_samples_per_candidate: int = 2, history_window: int = 20
) -> ModelRoutingPolicy:
    return ModelRoutingPolicy(
        catalog=_catalog("reliable-model", "economical-model"),
        policies=(
            RoutingPolicy(
                name="code-change",
                task_class="implementation",
                tool_call_budget=90,
                candidates=(
                    ModelProfile("reliable-model", "medium"),
                    ModelProfile("economical-model", "medium"),
                ),
                adaptive=AdaptiveRoutingSettings(
                    minimum_samples_per_candidate=minimum_samples_per_candidate,
                    history_window=history_window,
                    quality_floor=0.8,
                    prior_quality=0.75,
                    prior_weight=2.0,
                ),
            ),
        ),
    )


class AdaptiveModelRoutingTest(unittest.TestCase):
    def test_history_parser_requires_exact_true_and_finite_nonnegative_metrics(self) -> None:
        invalid_rows: tuple[tuple[str, dict[str, object] | None], ...] = (
            ("missing metrics_valid", None),
            ("false metrics_valid", {"metrics_valid": False}),
            ("integer metrics_valid", {"metrics_valid": 1}),
            ("string metrics_valid", {"metrics_valid": "true"}),
            ("negative input", {"input_tokens": -1}),
            ("negative cached input", {"cached_input_tokens": -1}),
            ("cached input exceeds input", {"cached_input_tokens": 2_000}),
            ("boolean output", {"output_tokens": True}),
            ("negative defects", {"defects_found": -1}),
            ("malformed duration", {"duration_sec": "not-a-number"}),
            ("nan duration", {"duration_sec": float("nan")}),
            ("infinite duration", {"duration_sec": float("inf")}),
        )

        for label, overrides in invalid_rows:
            with self.subTest(label=label):
                row = _history_row()
                if overrides is None:
                    del row["metrics_valid"]
                else:
                    row.update(overrides)
                self.assertIsNone(parse_routing_history_record(row))
                self.assertEqual(parse_routing_history_records([row]), ())

    def test_history_record_default_is_not_comparable(self) -> None:
        record = RoutingHistoryRecord(
            model="reliable-model",
            reasoning_effort="medium",
            attempt_status="completed",
            result_status="completed",
            input_tokens=1_000,
            cached_input_tokens=0,
            output_tokens=100,
            duration_sec=30.0,
            root_outcome="accepted",
            defects_found=0,
            catalog_source="models_cache.json",
            route="main",
            policy_name="code-change",
            task_class="implementation",
            selection_source="configured_fallback",
        )
        decision = _adaptive_routing().decision_for_policy(
            "code-change",
            route="main",
            history=(record,),
        )

        self.assertEqual(decision.candidate_scores[0].sample_count, 0)
        self.assertIn("invalid attempt metrics", decision.excluded_data_reasons)

    def test_arbitrary_named_policy_uses_configured_order_when_history_is_insufficient(
        self,
    ) -> None:
        routing = ModelRoutingPolicy(
            catalog=_catalog("invented-cached-model", "fallback-model"),
            policies=(
                RoutingPolicy(
                    name="implementation-fast-path",
                    task_class="implementation",
                    tool_call_budget=77,
                    candidates=(
                        ModelProfile("invented-cached-model", "ultra"),
                        ModelProfile("fallback-model", "medium"),
                    ),
                    adaptive=AdaptiveRoutingSettings(
                        minimum_samples_per_candidate=3,
                        history_window=20,
                        quality_floor=0.8,
                        prior_quality=0.75,
                        prior_weight=2.0,
                    ),
                ),
            ),
        )

        decision = routing.decision_for_policy("implementation-fast-path", history=())

        self.assertEqual(decision.requested_policy, "implementation-fast-path")
        self.assertEqual(decision.tool_call_budget, 77)
        self.assertEqual(decision.selection_source, "configured_fallback")
        self.assertEqual(
            decision.ladder,
            (
                ModelProfile("invented-cached-model", "ultra"),
                ModelProfile("fallback-model", "medium"),
            ),
        )
        self.assertIn(
            "insufficient comparative samples for every candidate", decision.excluded_data_reasons
        )

    def test_zero_comparable_samples_defaults_to_configured_fallback(self) -> None:
        routing = _adaptive_routing()

        decision = routing.decision_for_policy(
            "code-change",
            route="main",
            history=(),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        self.assertIn(
            "insufficient comparative samples for every candidate", decision.excluded_data_reasons
        )

    def test_enough_comparable_successes_can_promote_a_qualified_lower_cost_candidate(self) -> None:
        routing = ModelRoutingPolicy(
            catalog=_catalog("reliable-model", "economical-model"),
            policies=(
                RoutingPolicy(
                    name="code-change",
                    task_class="implementation",
                    tool_call_budget=90,
                    candidates=(
                        ModelProfile("reliable-model", "medium"),
                        ModelProfile("economical-model", "medium"),
                    ),
                    adaptive=AdaptiveRoutingSettings(
                        minimum_samples_per_candidate=3,
                        history_window=20,
                        quality_floor=0.8,
                        prior_quality=0.75,
                        prior_weight=2.0,
                    ),
                ),
            ),
        )

        decision = routing.decision_for_policy(
            "code-change",
            route="main",
            history=tuple(
                _history(
                    "reliable-model",
                    catalog_version=routing.catalog.version,
                )
                for _ in range(3)
            )
            + tuple(
                _history(
                    "economical-model",
                    catalog_version=routing.catalog.version,
                    duration_sec=10.0,
                )
                for _ in range(3)
            ),
        )

        self.assertEqual(decision.selection_source, "history")
        self.assertEqual(decision.ladder[0], ModelProfile("economical-model", "medium"))
        economical = next(
            score for score in decision.candidate_scores if score.model == "economical-model"
        )
        self.assertEqual(economical.sample_count, 3)
        self.assertGreaterEqual(economical.quality_score, 0.8)

    def test_one_comparable_sample_never_promotes_a_candidate(self) -> None:
        routing = _adaptive_routing()

        decision = routing.decision_for_policy(
            "code-change",
            route="main",
            history=(
                _history(
                    "economical-model",
                    catalog_version=routing.catalog.version,
                ),
            ),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        economical = next(
            score for score in decision.candidate_scores if score.model == "economical-model"
        )
        self.assertEqual(economical.sample_count, 1)
        self.assertIn("insufficient comparable samples", economical.exclusion_reasons)

    def test_two_accepted_samples_for_only_non_fallback_candidate_keep_configured_fallback(
        self,
    ) -> None:
        routing = _adaptive_routing()
        version = routing.catalog.version

        decision = routing.decision_for_policy(
            "code-change",
            route="main",
            history=tuple(
                _history(
                    "economical-model",
                    policy_name="code-change",
                    catalog_version=version,
                )
                for _ in range(2)
            ),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        self.assertIn(
            "insufficient comparative samples for every candidate",
            decision.excluded_data_reasons,
        )
        economical = next(
            score for score in decision.candidate_scores if score.model == "economical-model"
        )
        self.assertEqual(economical.sample_count, 2)

    def test_one_sample_for_every_candidate_keeps_configured_fallback(self) -> None:
        routing = _adaptive_routing()
        version = routing.catalog.version

        decision = routing.decision_for_policy(
            "code-change",
            route="main",
            history=(
                _history(
                    "reliable-model",
                    policy_name="code-change",
                    catalog_version=version,
                ),
                _history(
                    "economical-model",
                    policy_name="code-change",
                    catalog_version=version,
                ),
            ),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        reliable = next(
            score for score in decision.candidate_scores if score.model == "reliable-model"
        )
        economical = next(
            score for score in decision.candidate_scores if score.model == "economical-model"
        )
        self.assertEqual(reliable.sample_count, 1)
        self.assertEqual(economical.sample_count, 1)
        self.assertIn(
            "insufficient comparative samples for every candidate",
            decision.excluded_data_reasons,
        )

    def test_minimum_samples_for_every_candidate_enables_adaptive_selection(self) -> None:
        routing = _adaptive_routing()
        version = routing.catalog.version

        history = (
            _history(
                "reliable-model",
                policy_name="code-change",
                catalog_version=version,
            ),
            _history(
                "reliable-model",
                policy_name="code-change",
                catalog_version=version,
            ),
            _history(
                "economical-model",
                policy_name="code-change",
                catalog_version=version,
                duration_sec=10.0,
            ),
            _history(
                "economical-model",
                policy_name="code-change",
                catalog_version=version,
                duration_sec=10.0,
            ),
        )
        decision = routing.decision_for_policy("code-change", route="main", history=history)

        self.assertEqual(decision.selection_source, "history")
        self.assertEqual(
            decision.ladder[0],
            ModelProfile("economical-model", "medium"),
        )

    def test_reviewed_bad_observations_satisfy_sampling_gate_but_cannot_be_selected(self) -> None:
        routing = _adaptive_routing()
        version = routing.catalog.version

        decision = routing.decision_for_policy(
            "code-change",
            route="main",
            history=(
                *(
                    _history(
                        "reliable-model",
                        policy_name="code-change",
                        catalog_version=version,
                        duration_sec=10.0,
                    )
                    for _ in range(2)
                ),
                *(
                    _history(
                        "economical-model",
                        policy_name="code-change",
                        catalog_version=version,
                        root_outcome="rejected",
                    )
                    for _ in range(2)
                ),
            ),
        )

        self.assertEqual(decision.selection_source, "history")
        self.assertEqual(decision.ladder[0], ModelProfile("reliable-model", "medium"))
        reliable = next(
            score for score in decision.candidate_scores if score.model == "reliable-model"
        )
        economical = next(
            score for score in decision.candidate_scores if score.model == "economical-model"
        )
        self.assertEqual(economical.sample_count, 2)
        self.assertEqual(economical.success_count, 0)
        self.assertFalse(economical.eligible)
        self.assertIn("root rejection or defect", economical.exclusion_reasons)
        self.assertIn("quality floor not met", economical.exclusion_reasons)
        self.assertTrue(reliable.eligible)

    def test_missing_review_or_invalid_metrics_fail_closed_for_sampling(self) -> None:
        routing = _adaptive_routing()
        version = routing.catalog.version
        accepted = tuple(
            _history(
                "reliable-model",
                policy_name="code-change",
                catalog_version=version,
            )
            for _ in range(2)
        )

        for reason, root_outcome in (
            ("missing review", None),
            ("blank review", " "),
            ("unrecognized review", "defects"),
        ):
            with self.subTest(reason=reason):
                decision = routing.decision_for_policy(
                    "code-change",
                    route="main",
                    history=accepted
                    + tuple(
                        _history(
                            "economical-model",
                            policy_name="code-change",
                            catalog_version=version,
                            root_outcome=root_outcome,
                        )
                        for _ in range(2)
                    ),
                )
                self.assertEqual(decision.selection_source, "configured_fallback")
                self.assertEqual(decision.ladder[0], ModelProfile("reliable-model", "medium"))
                self.assertIn(
                    "insufficient comparative samples for every candidate",
                    decision.excluded_data_reasons,
                )
                expected_reason = (
                    "missing root review"
                    if root_outcome is None or not root_outcome.strip()
                    else "unrecognized root outcome"
                )
                self.assertIn(expected_reason, decision.excluded_data_reasons)
                economical = next(
                    score
                    for score in decision.candidate_scores
                    if score.model == "economical-model"
                )
                self.assertEqual(economical.sample_count, 0)
                self.assertIsNone(economical.quality_score)
                self.assertIsNone(economical.expected_api_usd)
                self.assertIsNone(economical.expected_duration_sec)
                self.assertIn("insufficient comparable samples", economical.exclusion_reasons)

        with self.subTest(reason="invalid metrics"):
            decision = routing.decision_for_policy(
                "code-change",
                route="main",
                history=accepted
                + tuple(
                    _history(
                        "economical-model",
                        policy_name="code-change",
                        catalog_version=version,
                        metrics_valid=False,
                    )
                    for _ in range(2)
                ),
            )
            self.assertEqual(decision.selection_source, "configured_fallback")
            self.assertIn(
                "insufficient comparative samples for every candidate",
                decision.excluded_data_reasons,
            )
            economical = next(
                score for score in decision.candidate_scores if score.model == "economical-model"
            )
            self.assertEqual(economical.sample_count, 0)

    def test_adaptive_settings_reject_too_few_samples(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "at least two comparable samples are required",
        ):
            AdaptiveRoutingSettings(
                minimum_samples_per_candidate=1,
                history_window=20,
                quality_floor=0.8,
                prior_quality=0.75,
                prior_weight=2.0,
            )

    def test_strict_history_metadata_is_filtered_before_history_window(self) -> None:
        routing = _adaptive_routing(history_window=2)
        version = routing.catalog.version

        history = (
            _history("economical-model", route=None, catalog_version=version),
            _history("economical-model", policy_name=None, catalog_version=version),
            _history("economical-model", task_class=None, catalog_version=version),
            _history("economical-model", selection_source=None, catalog_version=version),
            _history("economical-model", selection_source="explicit", catalog_version=version),
            _history("economical-model", route="other", catalog_version=version),
            _history("economical-model", policy_name="other-policy", catalog_version=version),
            _history("economical-model", task_class="research", catalog_version=version),
            _history("economical-model"),
            _history("economical-model", catalog_version=version),
            _history("economical-model", catalog_version=version),
        )

        decision = routing.decision_for_policy("code-change", route="main", history=history)

        self.assertEqual(decision.selection_source, "configured_fallback")
        economical = next(
            score for score in decision.candidate_scores if score.model == "economical-model"
        )
        self.assertEqual(economical.sample_count, 2)
        self.assertIn(
            "insufficient comparative samples for every candidate", decision.excluded_data_reasons
        )
        self.assertIn("missing route", decision.excluded_data_reasons)
        self.assertIn("missing policy", decision.excluded_data_reasons)
        self.assertIn("missing task class", decision.excluded_data_reasons)
        self.assertIn("missing selection source", decision.excluded_data_reasons)
        self.assertIn("explicit selection", decision.excluded_data_reasons)
        self.assertIn("unrelated route", decision.excluded_data_reasons)
        self.assertIn("unrelated policy", decision.excluded_data_reasons)
        self.assertIn("unrelated task class", decision.excluded_data_reasons)
        self.assertIn("missing catalog version", decision.excluded_data_reasons)

    def test_missing_root_review_never_promotes_a_candidate(self) -> None:
        routing = _adaptive_routing()

        decision = routing.decision_for_policy(
            "code-change",
            route="main",
            history=tuple(
                _history(
                    "economical-model",
                    root_outcome=None,
                    catalog_version=routing.catalog.version,
                )
                for _ in range(2)
            ),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        economical = next(
            score for score in decision.candidate_scores if score.model == "economical-model"
        )
        self.assertIn("missing root review", economical.exclusion_reasons)

    def test_rejection_or_defect_prevents_a_cheap_candidate_from_promotion(self) -> None:
        routing = ModelRoutingPolicy(
            catalog=_catalog("reliable-model", "cheap-model"),
            policies=(
                RoutingPolicy(
                    name="reviewed-change",
                    task_class="implementation",
                    tool_call_budget=90,
                    candidates=(
                        ModelProfile("reliable-model", "medium"),
                        ModelProfile("cheap-model", "medium"),
                    ),
                    adaptive=AdaptiveRoutingSettings(
                        minimum_samples_per_candidate=3,
                        history_window=20,
                        quality_floor=0.8,
                        prior_quality=0.75,
                        prior_weight=2.0,
                    ),
                ),
            ),
        )

        decision = routing.decision_for_policy(
            "reviewed-change",
            route="main",
            history=(
                _history(
                    "cheap-model",
                    policy_name="reviewed-change",
                    catalog_version=routing.catalog.version,
                ),
                _history(
                    "cheap-model",
                    policy_name="reviewed-change",
                    catalog_version=routing.catalog.version,
                ),
                _history(
                    "cheap-model",
                    policy_name="reviewed-change",
                    defects_found=1,
                    catalog_version=routing.catalog.version,
                ),
            ),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        self.assertEqual(decision.ladder[0], ModelProfile("reliable-model", "medium"))
        cheap = next(score for score in decision.candidate_scores if score.model == "cheap-model")
        self.assertIn("root rejection or defect", cheap.exclusion_reasons)

    def test_unpriced_candidate_is_not_promoted_without_an_explicit_missing_price_guardrail(
        self,
    ) -> None:
        routing = ModelRoutingPolicy(
            catalog=_catalog("reliable-model", "unpriced-model", unpriced=("unpriced-model",)),
            policies=(
                RoutingPolicy(
                    name="safe-routing",
                    task_class="implementation",
                    tool_call_budget=90,
                    candidates=(
                        ModelProfile("reliable-model", "medium"),
                        ModelProfile("unpriced-model", "medium"),
                    ),
                    adaptive=AdaptiveRoutingSettings(
                        minimum_samples_per_candidate=3,
                        history_window=20,
                        quality_floor=0.8,
                        prior_quality=0.75,
                        prior_weight=2.0,
                    ),
                ),
            ),
        )

        decision = routing.decision_for_policy(
            "safe-routing",
            route="main",
            history=tuple(
                _history(
                    "unpriced-model",
                    policy_name="safe-routing",
                    catalog_version=routing.catalog.version,
                )
                for _ in range(3)
            ),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        unpriced = next(
            score for score in decision.candidate_scores if score.model == "unpriced-model"
        )
        self.assertIn("missing current price", unpriced.exclusion_reasons)


if __name__ == "__main__":
    unittest.main()
