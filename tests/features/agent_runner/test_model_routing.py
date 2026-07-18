from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.model_catalog import ModelCatalog
from agent_control_plane.features.agent_runner.lib.model_routing import (
    ModelProfile,
    ModelRoutingPolicy,
)


def _catalog(*models: dict[str, object]) -> ModelCatalog:
    with tempfile.TemporaryDirectory() as temp:
        cache_path = Path(temp) / "models_cache.json"
        cache_path.write_text(json.dumps({"models": models}), encoding="utf-8")
        return ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)


class ModelRoutingPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ModelRoutingPolicy(
            mechanical=ModelProfile("gpt-5.6-luna", "low"),
            balanced=ModelProfile("gpt-5.6-luna", "medium"),
            deep=ModelProfile("gpt-5.6-terra", "medium"),
            catalog=_catalog(
                {
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": ["none", "low", "medium", "high", "xhigh"],
                },
                {
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": ["none", "low", "medium", "high", "xhigh"],
                },
            ),
        )

    def test_mechanical_starts_on_luna_and_escalates_to_deep_model(self) -> None:
        ladder = self.policy.ladder_for_tier("mechanical")

        self.assertEqual(
            [(profile.model, profile.reasoning_effort) for profile in ladder],
            [
                ("gpt-5.6-luna", "low"),
                ("gpt-5.6-terra", "medium"),
            ],
        )

    def test_deep_tier_does_not_add_a_duplicate_fallback(self) -> None:
        ladder = self.policy.ladder_for_tier("deep")

        self.assertEqual(ladder, (ModelProfile("gpt-5.6-terra", "medium"),))

    def test_explicit_model_remains_fixed(self) -> None:
        ladder = self.policy.ladder_for_explicit_model("custom-model", "high")

        self.assertEqual(ladder, (ModelProfile("custom-model", "high"),))

    def test_managed_model_rejects_unsupported_reasoning_effort(self) -> None:
        for effort in ("minimal", "max", "turbo"):
            with (
                self.subTest(effort=effort),
                self.assertRaisesRegex(ValueError, "does not support reasoning effort"),
            ):
                self.policy.ladder_for_explicit_model("gpt-5.6-luna", effort)

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.policy.ladder_for_explicit_model("gpt-5.6-luna", "")

    def test_custom_model_effort_remains_backend_defined(self) -> None:
        ladder = self.policy.ladder_for_explicit_model("custom-model", "minimal")

        self.assertEqual(ladder, (ModelProfile("custom-model", "minimal"),))

    def test_invalid_managed_tier_is_rejected_when_selected(self) -> None:
        policy = ModelRoutingPolicy(
            mechanical=ModelProfile("gpt-5.6-luna", "minimal"),
            balanced=ModelProfile("gpt-5.6-terra", "medium"),
            deep=ModelProfile("gpt-5.6-terra", "high"),
            catalog=_catalog(
                {
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": ["none", "low", "medium", "high", "xhigh"],
                },
                {
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": ["none", "low", "medium", "high", "xhigh"],
                },
            ),
        )

        with self.assertRaisesRegex(ValueError, "does not support reasoning effort 'minimal'"):
            policy.ladder_for_tier("mechanical")

    def test_only_classified_model_capability_partial_escalates(self) -> None:
        self.assertTrue(
            self.policy.should_escalate(
                runner_status="completed",
                result_status="partial",
                has_next=True,
                escalation_classification="model_capability",
            )
        )
        for classification in (None, "infrastructure", "quota", "spawn", "tooling"):
            with self.subTest(classification=classification):
                self.assertFalse(
                    self.policy.should_escalate(
                        runner_status="completed",
                        result_status="partial",
                        has_next=True,
                        escalation_classification=classification,
                    )
                )
        self.assertFalse(
            self.policy.should_escalate(
                runner_status="capacity",
                result_status=None,
                has_next=True,
            )
        )
        self.assertFalse(
            self.policy.should_escalate(
                runner_status="completed",
                result_status="completed",
                has_next=True,
            )
        )
        self.assertFalse(
            self.policy.should_escalate(
                runner_status="capacity",
                result_status=None,
                has_next=False,
            )
        )

    def test_unknown_tier_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "quality tier"):
            self.policy.ladder_for_tier("cheap")

    def test_known_catalog_model_validates_declared_future_efforts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "future-codex",
                                "supported_reasoning_levels": [
                                    {"effort": "medium", "description": "Balanced"},
                                    {"effort": "ultra", "description": "Future"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            policy = ModelRoutingPolicy(
                mechanical=ModelProfile("future-codex", "ultra"),
                balanced=ModelProfile("future-codex", "medium"),
                deep=ModelProfile("future-codex", "ultra"),
                catalog=ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0),
            )

            self.assertEqual(
                policy.ladder_for_explicit_model("future-codex", "ultra"),
                (ModelProfile("future-codex", "ultra"),),
            )
            with self.assertRaisesRegex(ValueError, "Expected one of: medium, ultra"):
                policy.ladder_for_explicit_model("future-codex", "high")

    def test_default_selector_uses_visible_priority_and_cache_order(self) -> None:
        policy = ModelRoutingPolicy(
            mechanical=ModelProfile("default", "medium"),
            balanced=ModelProfile("default", "medium"),
            deep=ModelProfile("default", "medium"),
            catalog=_catalog(
                {
                    "slug": "hidden-priority-one",
                    "visibility": "hide",
                    "priority": 1,
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "priority-tie-first",
                    "visibility": "list",
                    "priority": 2,
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "priority-tie-second",
                    "visibility": "list",
                    "priority": 2,
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "lower-priority",
                    "visibility": "list",
                    "priority": 4,
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
            ),
        )

        self.assertEqual(
            policy.ladder_for_tier("balanced"),
            (ModelProfile("priority-tie-first", "medium"),),
        )
        unsupported_effort_policy = ModelRoutingPolicy(
            mechanical=ModelProfile("default", "high"),
            balanced=ModelProfile("default", "high"),
            deep=ModelProfile("default", "high"),
            catalog=policy.catalog,
        )

        with self.assertRaisesRegex(ValueError, "does not support reasoning effort 'high'"):
            unsupported_effort_policy.ladder_for_tier("balanced")

    def test_default_selector_fails_closed_when_the_catalog_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            catalog = ModelCatalog.load(
                cache_path=Path(temp) / "models_cache.json",
                max_cache_age_sec=60.0,
            )
        policy = ModelRoutingPolicy(
            mechanical=ModelProfile("default", "low"),
            balanced=ModelProfile("default", "medium"),
            deep=ModelProfile("default", "medium"),
            catalog=catalog,
        )

        with self.assertRaisesRegex(ValueError, "catalog is missing"):
            policy.ladder_for_tier("deep")


if __name__ == "__main__":
    unittest.main()
