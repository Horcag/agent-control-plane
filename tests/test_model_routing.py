from __future__ import annotations

import unittest

from agent_control_plane.features.agent_runner.lib.model_routing import (
    ModelProfile,
    ModelRoutingPolicy,
)


class ModelRoutingPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ModelRoutingPolicy(
            mechanical=ModelProfile("gpt-5.6-luna", "low"),
            balanced=ModelProfile("gpt-5.6-luna", "medium"),
            deep=ModelProfile("gpt-5.6-terra", "medium"),
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
        for effort in ("minimal", "max", "turbo", ""):
            with (
                self.subTest(effort=effort),
                self.assertRaisesRegex(ValueError, "does not support reasoning effort"),
            ):
                self.policy.ladder_for_explicit_model("gpt-5.6-luna", effort)

    def test_custom_model_effort_remains_backend_defined(self) -> None:
        ladder = self.policy.ladder_for_explicit_model("custom-model", "minimal")

        self.assertEqual(ladder, (ModelProfile("custom-model", "minimal"),))

    def test_invalid_managed_tier_is_rejected_when_selected(self) -> None:
        policy = ModelRoutingPolicy(
            mechanical=ModelProfile("gpt-5.6-luna", "minimal"),
            balanced=ModelProfile("gpt-5.6-terra", "medium"),
            deep=ModelProfile("gpt-5.6-terra", "high"),
        )

        with self.assertRaisesRegex(ValueError, "does not support reasoning effort 'minimal'"):
            policy.ladder_for_tier("mechanical")

    def test_nonfinal_result_and_capacity_escalate_but_completed_does_not(self) -> None:
        self.assertTrue(
            self.policy.should_escalate(
                runner_status="completed",
                result_status="partial",
                has_next=True,
            )
        )
        self.assertTrue(
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


if __name__ == "__main__":
    unittest.main()
