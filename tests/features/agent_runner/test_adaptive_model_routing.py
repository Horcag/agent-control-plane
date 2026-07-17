from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.model_catalog import (
    CatalogModelMetadata,
    CatalogRate,
    ModelCatalog,
)
from agent_control_plane.features.agent_runner.lib.model_routing import (
    AdaptiveRoutingSettings,
    ModelProfile,
    ModelRoutingPolicy,
    RoutingHistoryRecord,
    RoutingPolicy,
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
    )


class AdaptiveModelRoutingTest(unittest.TestCase):
    def test_arbitrary_named_policy_uses_configured_order_when_history_is_insufficient(self) -> None:
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
        self.assertIn("insufficient comparable history", decision.excluded_data_reasons)

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
            history=tuple(_history("economical-model") for _ in range(3)),
        )

        self.assertEqual(decision.selection_source, "history")
        self.assertEqual(decision.ladder[0], ModelProfile("economical-model", "medium"))
        economical = next(score for score in decision.candidate_scores if score.model == "economical-model")
        self.assertEqual(economical.sample_count, 3)
        self.assertGreaterEqual(economical.quality_score, 0.8)

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
            history=(
                _history("cheap-model"),
                _history("cheap-model"),
                _history("cheap-model", defects_found=1),
            ),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        self.assertEqual(decision.ladder[0], ModelProfile("reliable-model", "medium"))
        cheap = next(score for score in decision.candidate_scores if score.model == "cheap-model")
        self.assertIn("root rejection or defect", cheap.exclusion_reasons)

    def test_unpriced_candidate_is_not_promoted_without_an_explicit_missing_price_guardrail(self) -> None:
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
            history=tuple(_history("unpriced-model") for _ in range(3)),
        )

        self.assertEqual(decision.selection_source, "configured_fallback")
        unpriced = next(score for score in decision.candidate_scores if score.model == "unpriced-model")
        self.assertIn("missing current price", unpriced.exclusion_reasons)


if __name__ == "__main__":
    unittest.main()
