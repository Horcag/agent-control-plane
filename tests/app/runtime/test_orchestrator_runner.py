from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

from agent_control_plane.app.runtime.orchestrator import (
    AgentControlPlane,
    PolicyError,
    StartOptions,
)
from agent_control_plane.entities.job import AttemptMetrics
from agent_control_plane.entities.plan import PlanExecutionSpec, PlanTaskDefinition
from agent_control_plane.features.agent_runner import (
    AGY_BACKEND,
    CODEX_BACKEND,
    ModelProfile,
    QuotaDecision,
)
from agent_control_plane.features.agent_runner.lib.pty_runner import AgyRunResult
from agent_control_plane.features.agent_runner.lib.result_detector import inspect_result
from agent_control_plane.shared.config import (
    CodexAdaptiveRoutingConfig,
    CodexModelCatalogConfig,
    CodexModelMetadataConfig,
    CodexQuotaDomainConfig,
    CodexRoutingCandidateConfig,
    CodexRoutingPolicyConfig,
    ControlConfig,
    ControlDefaults,
    NativeQualityGateConfig,
    RouteConfig,
    SlotConfig,
)


class OrchestratorRunnerResultTest(unittest.TestCase):
    def test_smoke_reports_advertised_default_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            config = _config(root, workspace)
            control = AgentControlPlane(config)

            smoke = control.smoke()

            self.assertEqual(smoke["status"], "failed")
            failure = next(
                item for item in smoke["failures"] if item["code"] == "advertised_default_mismatch"
            )
            self.assertEqual(failure["effective_initial_profile"]["model"], "gpt-5.6-terra")

    def test_smoke_premium_collapse_is_exact_and_diagnostics_are_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            base = _config(root, workspace)
            premium_catalog = replace(
                base.model_catalog,
                models=tuple(replace(model, premium=True) for model in base.model_catalog.models),
            )
            defaults = replace(
                base.defaults,
                codex_model="gpt-5.6-terra",
                codex_mechanical_model="gpt-5.6-terra",
                codex_balanced_model="gpt-5.6-terra",
                codex_deep_model="gpt-5.6-terra",
            )
            collapsed = AgentControlPlane(
                replace(base, defaults=defaults, model_catalog=premium_catalog)
            )

            smoke = collapsed.smoke()
            self.assertEqual(smoke["status"], "failed")
            self.assertIn(
                "all_policy_initial_profiles_premium",
                {item["code"] for item in smoke["failures"]},
            )
            self.assertEqual(
                set(smoke["codex_quality_profiles"]), {"mechanical", "balanced", "deep"}
            )
            for profiles in smoke["codex_quality_profiles"].values():
                self.assertTrue(profiles)
                self.assertEqual(set(profiles[0]), {"model", "reasoning_effort", "premium"})

            different = AgentControlPlane(replace(base, model_catalog=premium_catalog))
            self.assertNotIn(
                "all_policy_initial_profiles_premium",
                {item["code"] for item in different.smoke()["failures"]},
            )

    def test_smoke_unknown_catalog_metadata_is_never_nonpremium(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            base = _config(root, workspace)
            control = AgentControlPlane(
                replace(base, model_catalog=replace(base.model_catalog, models=()))
            )

            smoke = control.smoke()

            inspection = {model["model"]: model for model in smoke["codex_model_catalog"]["models"]}
            self.assertIsNone(inspection["gpt-5.6-luna"]["premium"])
            self.assertEqual(inspection["gpt-5.6-luna"]["premium_state"], "unknown")
            self.assertIsNone(smoke["codex_quality_profiles"]["mechanical"][0]["premium"])
            self.assertNotIn(
                "all_policy_initial_profiles_premium", {item["code"] for item in smoke["failures"]}
            )

    def test_model_routing_explain_states_coordinator_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))

            payload = control.model_routing_explain("deep", "main")

            self.assertIn("model_control_scope", payload)
            self.assertIn("coordinating", payload["model_control_scope"].lower())
            self.assertNotIn("parent_model", payload)

    def test_run_job_delegates_to_the_execution_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            expected = object()

            with patch.object(control.job_execution, "run_job", return_value=expected) as run_job:
                actual = control.run_job("job-1", worker_instance_id="worker-1")

            self.assertIs(actual, expected)
            run_job.assert_called_once_with("job-1", worker_instance_id="worker-1")

    def test_workspace_access_precedence_and_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            base_config = _config(root, workspace)
            route = base_config.routes["main"]
            config = replace(
                base_config,
                defaults=replace(base_config.defaults, workspace_access="native"),
                routes=MappingProxyType({"main": replace(route, workspace_access="ide_mcp")}),
            )
            control = AgentControlPlane(config)
            _brief(config.coordination_root, "route-mode")
            _brief(config.coordination_root, "job-mode")

            with patch.object(control, "_launch_worker", return_value=123):
                route_job = control.start_job(StartOptions(task_id="route-mode", route="main"))
                job_override = control.start_job(
                    StartOptions(
                        task_id="job-mode",
                        route="main",
                        workspace_access="native",
                    )
                )

            self.assertEqual(route_job.workspace_access, "ide_mcp")
            self.assertEqual(job_override.workspace_access, "native")
            control.store.update_job(job_override.job_id, status="completed")
            self.assertEqual(
                control.status_job(job_override.job_id)["workspace_access"],
                "native",
            )
            self.assertEqual(
                control.status_job(job_override.job_id)["native_quality"]["policy"],
                "worker",
            )
            self.assertEqual(
                control.status_job(job_override.job_id)["native_quality"]["contract_state"],
                "matches",
            )
            self.assertEqual(
                control.summary_job(job_override.job_id)["workspace_access"],
                "native",
            )
            smoke = control.smoke()
            self.assertEqual(smoke["workspace_access"], "native")
            self.assertEqual(smoke["native_quality_policy"], "worker")
            self.assertEqual(smoke["routes"]["main"]["workspace_access"], "ide_mcp")
            self.assertEqual(smoke["routes"]["main"]["native_quality_policy"], "worker")

    def test_invalid_and_agy_native_modes_are_rejected_before_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "invalid-mode")
            _brief(control.config.coordination_root, "agy-native")

            with patch.object(control, "_launch_worker", return_value=123) as launch:
                with self.assertRaisesRegex(PolicyError, "workspace_access"):
                    control.start_job(
                        StartOptions(
                            task_id="invalid-mode",
                            route="main",
                            workspace_access="",
                        )
                    )
                with self.assertRaisesRegex(PolicyError, "agy backend"):
                    control.start_job(
                        StartOptions(
                            task_id="agy-native",
                            route="main",
                            backend=AGY_BACKEND,
                            workspace_access="native",
                        )
                    )

            launch.assert_not_called()
            self.assertEqual(control.store.list_jobs(), [])

    def test_automatic_codex_job_rejects_a_missing_catalog_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            config = _config(root, workspace)
            config = replace(
                config,
                model_catalog=replace(
                    config.model_catalog,
                    cache_path=root / "missing-models_cache.json",
                ),
            )
            control = AgentControlPlane(config)
            _brief(control.config.coordination_root, "missing-catalog")

            with (
                patch.object(control, "_launch_worker", return_value=123) as launch,
                self.assertRaisesRegex(PolicyError, "catalog is missing"),
            ):
                control.start_job(StartOptions(task_id="missing-catalog", route="main"))

            launch.assert_not_called()
            self.assertEqual(control.store.list_jobs(), [])

    def test_automatic_codex_one_sample_stays_on_fallback_and_records_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_adaptive_config(root, workspace))
            _brief(control.config.coordination_root, "adaptive-fallback")
            history = [
                _routing_history_mapping(control, "gpt-5.6-luna"),
                {"model": None, "reasoning_effort": "low"},
            ]

            with (
                patch.object(
                    control.store, "routing_history", return_value=history
                ) as history_read,
                patch.object(control, "_launch_worker", return_value=123),
            ):
                job = control.start_job(
                    StartOptions(
                        task_id="adaptive-fallback",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            history_read.assert_called_once_with()
            self.assertEqual(
                (job.codex_model, job.codex_reasoning_effort, job.codex_tool_call_budget),
                ("gpt-5.6-terra", "low", 91),
            )
            decision = control.store.routing_decision(job.job_id)
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual(decision["selection_source"], "configured_fallback")
            self.assertEqual(
                decision["ladder"][0],
                {"model": "gpt-5.6-terra", "reasoning_effort": "low"},
            )
            self.assertEqual(
                sum(
                    level == "routing_decision"
                    for _, level, _ in control.store.recent_events(job.job_id)
                ),
                1,
            )

    def test_routing_decision_persistence_failure_blocks_created_job_before_worker_launch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_adaptive_config(root, workspace))
            _brief(control.config.coordination_root, "decision-persist-failure")

            with (
                patch.object(
                    control.store,
                    "record_routing_decision",
                    side_effect=RuntimeError("routing DB unavailable"),
                ),
                patch.object(control, "_launch_worker", return_value=123) as launch,
                self.assertRaisesRegex(PolicyError, "Could not persist routing decision"),
            ):
                control.start_job(
                    StartOptions(
                        task_id="decision-persist-failure",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            launch.assert_not_called()
            jobs = control.store.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].status, "blocked")
            self.assertIsNotNone(jobs[0].finished_at)
            self.assertIn("routing DB unavailable", jobs[0].last_error or "")

    def test_automatic_codex_history_can_promote_and_persist_reordered_ladder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_adaptive_config(root, workspace))
            _brief(control.config.coordination_root, "adaptive-history")
            history = [
                _routing_history_mapping(control, "gpt-5.6-luna"),
                _routing_history_mapping(control, "gpt-5.6-luna"),
                {
                    **_routing_history_mapping(control, "gpt-5.6-terra"),
                    "duration_sec": 2.0,
                },
                {
                    **_routing_history_mapping(control, "gpt-5.6-terra"),
                    "duration_sec": 2.0,
                },
            ]

            with (
                patch.object(control.store, "routing_history", return_value=history),
                patch.object(control, "_launch_worker", return_value=123),
            ):
                job = control.start_job(
                    StartOptions(
                        task_id="adaptive-history",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            decision = control.store.routing_decision(job.job_id)
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual(job.codex_model, "gpt-5.6-luna")
            self.assertEqual(decision["selection_source"], "history")
            self.assertEqual(
                decision["ladder"],
                [
                    {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
                    {"model": "gpt-5.6-terra", "reasoning_effort": "low"},
                ],
            )

    def test_restarted_control_plane_uses_persisted_ladder_after_policy_reorder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            config = _adaptive_config(root, workspace)
            control = AgentControlPlane(config)
            _brief(config.coordination_root, "adaptive-restart")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="adaptive-restart",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            reordered_policy = replace(
                config.routing_policies[0],
                candidates=(
                    CodexRoutingCandidateConfig("gpt-5.6-luna", "low"),
                    CodexRoutingCandidateConfig("gpt-5.6-terra", "low"),
                ),
            )
            restarted = AgentControlPlane(replace(config, routing_policies=(reordered_policy,)))
            worker_ladder = restarted.job_execution._model_ladder_for_job(
                restarted.store.get_job(job.job_id)
            )

            self.assertEqual(
                worker_ladder,
                (
                    ModelProfile("gpt-5.6-terra", "low"),
                    ModelProfile("gpt-5.6-luna", "low"),
                ),
            )

    def test_restart_falls_back_to_stored_first_profile_when_current_policy_contract_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            config = _adaptive_config(root, workspace)
            control = AgentControlPlane(config)
            _brief(config.coordination_root, "adaptive-contract-restart")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="adaptive-contract-restart",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            expected = (ModelProfile("gpt-5.6-terra", "low"),)
            changed_policy = replace(config.routing_policies[0], tool_call_budget=92)
            changed_config = replace(config, routing_policies=(changed_policy,))
            restarted = AgentControlPlane(changed_config)

            self.assertEqual(
                restarted.job_execution._model_ladder_for_job(restarted.store.get_job(job.job_id)),
                expected,
            )

            missing_policy = replace(
                config.routing_policies[0],
                name="replacement-policy",
            )
            missing_policy_config = replace(
                config,
                defaults=replace(config.defaults, codex_quality_tier="replacement-policy"),
                routing_policies=(missing_policy,),
            )
            restarted_without_policy = AgentControlPlane(missing_policy_config)
            self.assertEqual(
                restarted_without_policy.job_execution._model_ladder_for_job(
                    restarted_without_policy.store.get_job(job.job_id)
                ),
                expected,
            )

    def test_persisted_automatic_decision_requires_exact_job_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_adaptive_config(root, workspace))
            _brief(control.config.coordination_root, "adaptive-contract")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="adaptive-contract",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            expected = (ModelProfile(job.codex_model or "", job.codex_reasoning_effort or ""),)
            decision = control.store.routing_decision(job.job_id)
            self.assertIsNotNone(decision)
            assert decision is not None
            cases = (
                {"requested_policy": "another-policy"},
                {"tool_call_budget": (job.codex_tool_call_budget or 0) + 1},
                {"catalog": {"source": "", "version": decision["catalog"]["version"]}},
                {
                    "catalog": {
                        "source": decision["catalog"]["source"],
                        "version": "other",
                    }
                },
                {"task_class": "other"},
                {"selection_source": "history", "configured_fallback": True},
            )

            for changes in cases:
                payload = json.loads(json.dumps(decision))
                payload.update(changes)
                control.store.record_routing_decision(job.job_id, payload)
                self.assertEqual(
                    control.job_execution._model_ladder_for_job(control.store.get_job(job.job_id)),
                    expected,
                )

    def test_automatic_legacy_job_with_missing_or_bad_decision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            job = _create_job(
                control,
                root,
                workspace,
                "legacy-routing",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.6-luna",
                codex_reasoning_effort="low",
                codex_quality_tier="mechanical",
            )
            expected = (ModelProfile("gpt-5.6-luna", "low"),)

            self.assertEqual(control.job_execution._model_ladder_for_job(job), expected)
            control.store.record_routing_decision(
                job.job_id,
                {
                    "event": "routing_decision",
                    "route": "main",
                    "ladder": [{"model": "gpt-5.6-terra", "reasoning_effort": "medium"}],
                },
            )
            self.assertEqual(
                control.job_execution._model_ladder_for_job(control.store.get_job(job.job_id)),
                expected,
            )

    def test_explicit_codex_profile_bypasses_adaptive_selection_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_adaptive_config(root, workspace))
            _brief(control.config.coordination_root, "explicit-profile")

            with (
                patch.object(control.store, "routing_history") as history_read,
                patch.object(control, "_launch_worker", return_value=123),
            ):
                job = control.start_job(
                    StartOptions(
                        task_id="explicit-profile",
                        route="main",
                        backend=CODEX_BACKEND,
                        codex_model="gpt-5.6-terra",
                        codex_reasoning_effort="high",
                    )
                )

            history_read.assert_not_called()
            self.assertEqual(job.codex_model, "gpt-5.6-terra")
            self.assertEqual(job.codex_reasoning_effort, "high")
            self.assertIsNone(job.codex_quality_tier)
            self.assertIsNone(control.store.routing_decision(job.job_id))

    def test_controller_native_quality_requires_checkpointed_slot_and_persists_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_path = _git_repo(root / "repo", "main")
            slot_path = _git_repo(root / "slots" / "main-1", "main")
            base = _config_with_slot(root, route_path, slot_path)
            route = replace(
                base.routes["main"],
                native_quality_policy="controller",
                native_quality_max_parallel=2,
                native_quality_gates=(
                    NativeQualityGateConfig(
                        name="tests",
                        command=("python", "-m", "pytest"),
                        run_on="controller",
                    ),
                ),
            )
            preserve = replace(base, routes=MappingProxyType({"main": route}))
            control = AgentControlPlane(preserve)
            _brief(control.config.coordination_root, "preserve")

            with self.assertRaisesRegex(PolicyError, "checkpointed slot"):
                control.start_job(
                    StartOptions(
                        task_id="preserve",
                        route="main",
                        slot="main-1",
                        workspace_access="native",
                    )
                )

            checkpoint = replace(
                preserve,
                defaults=replace(preserve.defaults, terminal_slot_policy="checkpoint"),
            )
            control = AgentControlPlane(checkpoint)
            _brief(control.config.coordination_root, "strict")
            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="strict",
                        route="main",
                        slot="main-1",
                        workspace_access="native",
                    )
                )

            contract = job.run_dir / "native-quality-contract.json"
            self.assertTrue(contract.is_file())
            contract_text = contract.read_text(encoding="utf-8")
            self.assertIn('"policy": "controller"', contract_text)
            self.assertIn('"max_parallel": 2', contract_text)
            self.assertIn('"run_on": "controller"', contract_text)
            prompt_text = job.prompt_path.read_text(encoding="utf-8")
            self.assertIn("Controller-executed gates (maximum 2 in parallel): tests", prompt_text)
            self.assertNotIn("python -m pytest", prompt_text)

    def test_native_slot_skips_only_ide_module_provisioning(self) -> None:
        for workspace_access, expected_ide_calls in (("native", 0), ("ide_mcp", 1)):
            with (
                self.subTest(workspace_access=workspace_access),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                route_path = _git_repo(root / "repo", "main")
                slot_path = _git_repo(root / "slots" / "main-1", "main")
                control = AgentControlPlane(_config_with_slot(root, route_path, slot_path))
                _brief(control.config.coordination_root, "slot-task")

                with (
                    patch.object(control.slots, "ensure_ide_root_module") as ensure_ide_root,
                    patch.object(control.slots, "prepare_slot") as prepare_slot,
                    patch.object(control, "_launch_worker", return_value=123),
                ):
                    control.start_job(
                        StartOptions(
                            task_id="slot-task",
                            route="main",
                            slot="main-1",
                            backend=CODEX_BACKEND,
                            workspace_access=workspace_access,
                        )
                    )

                self.assertEqual(ensure_ide_root.call_count, expected_ide_calls)
                prepare_slot.assert_called_once_with("main-1")

    def test_native_read_only_progress_does_not_require_a_progress_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "native-review")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="native-review",
                        route="main",
                        workspace_access="native",
                        read_only=True,
                    )
                )

            progress = (job.result_path.parent / "agent-progress.md").read_text(encoding="utf-8")
            self.assertIn("native read-only tools", progress)
            self.assertIn("must not update this progress file", progress)

    def test_native_runner_spec_disables_agentbridge_and_allows_raw_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            base_config = _config(root, workspace)
            config = replace(
                base_config,
                defaults=replace(
                    base_config.defaults,
                    codex_disabled_mcp_servers=("custom", "agentbridge_idea_64343"),
                    codex_forbidden_tool_markers=("raw_exec", "web_search"),
                ),
                routes=MappingProxyType(
                    {
                        "main": replace(
                            base_config.routes["main"],
                            ide_mcp_server="agentbridge_idea_64343",
                        ),
                        "secondary": replace(
                            base_config.routes["main"],
                            name="secondary",
                            ide_mcp_server="agentbridge_idea_9999",
                        ),
                    }
                ),
            )
            control = AgentControlPlane(config)
            _brief(config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-native",
                backend=CODEX_BACKEND,
                workspace_access="native",
            )
            runner = _CapturingCompletedRunner()

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=runner,
            ):
                finished = control.run_job(job.job_id)

            spec = runner.specs[0]
            self.assertEqual(finished.status, "completed")
            self.assertEqual(spec.workspace_access, "native")
            self.assertIsNone(spec.codex_terminal_tab_name)
            self.assertEqual(spec.codex_forbidden_tool_markers, ("web_search",))
            self.assertEqual(
                spec.codex_disabled_mcp_servers,
                (
                    "custom",
                    "agentbridge_idea_64343",
                    "agentbridge_idea_9999",
                    "agentbridge_dataspell_8643",
                    "agentbridge_idea_8644",
                ),
            )

    def test_agy_model_override_is_persisted_for_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-agy-model")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="task-agy-model",
                        route="main",
                        backend=AGY_BACKEND,
                        agy_model="Gemini 3.5 Flash (High)",
                    )
                )

            self.assertEqual(job.agy_model, "Gemini 3.5 Flash (High)")
            self.assertIsNone(job.codex_model)
            self.assertIsNone(job.codex_reasoning_effort)

    def test_agy_model_override_is_rejected_for_codex_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-wrong-model-option")

            with self.assertRaisesRegex(PolicyError, "agy-model"):
                control.start_job(
                    StartOptions(
                        task_id="task-wrong-model-option",
                        route="main",
                        backend=CODEX_BACKEND,
                        agy_model="Gemini 3.5 Flash (High)",
                    )
                )

    def test_unsupported_managed_codex_effort_is_rejected_before_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-unsupported-effort")

            with (
                patch.object(control, "_launch_worker", return_value=123) as launch,
                self.assertRaisesRegex(PolicyError, "does not support reasoning effort 'minimal'"),
            ):
                control.start_job(
                    StartOptions(
                        task_id="task-unsupported-effort",
                        route="main",
                        backend=CODEX_BACKEND,
                        codex_model="gpt-5.6-luna",
                        codex_reasoning_effort="minimal",
                    )
                )

            launch.assert_not_called()
            self.assertEqual(control.store.list_jobs(), [])

    def test_start_job_initializes_codex_coordination_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="task-1",
                        route="main",
                        backend=CODEX_BACKEND,
                    )
                )

            task_dir = control.config.coordination_root / "tasks" / "task-1"
            progress_text = (task_dir / "agent-progress.md").read_text(encoding="utf-8")
            result_text = (task_dir / "result.md").read_text(encoding="utf-8")
            result_state = inspect_result(task_dir / "result.md", 0.0)

            self.assertEqual(job.status, "queued")
            self.assertIn(job.job_id, progress_text)
            self.assertIn(str(job.workspace_path), progress_text)
            self.assertIn("Awaiting agent execution", result_text)
            self.assertFalse(result_state.done)

    def test_start_job_binds_retry_task_id_to_logical_plan_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            control.create_plan(
                plan_id="transfer",
                title="Transfer",
                tasks=(PlanTaskDefinition("schema", "Transfer schema"),),
            )
            _brief(control.config.coordination_root, "schema-repair-r2")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="schema-repair-r2",
                        route="main",
                        backend=CODEX_BACKEND,
                        plan_id="transfer",
                        plan_task_id="schema",
                    )
                )

            snapshot = control.plan_snapshot("transfer")
            self.assertEqual(snapshot["running"][0]["task_id"], "schema")
            self.assertEqual(snapshot["running"][0]["job_id"], job.job_id)

    def test_plan_dispatch_materializes_private_brief_and_starts_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            control.create_plan(
                plan_id="dispatch",
                title="Dispatch",
                tasks=(
                    PlanTaskDefinition(
                        "schema",
                        "Schema",
                        execution=PlanExecutionSpec(
                            route="main",
                            brief="Implement only the schema change.",
                            backend=CODEX_BACKEND,
                            workspace_access="native",
                            codex_quality_tier="mechanical",
                        ),
                    ),
                ),
            )

            with patch.object(control, "_launch_worker", return_value=123):
                first = control.dispatch_plan("dispatch", max_jobs=1)
                second = control.dispatch_plan("dispatch", max_jobs=1)

            dispatched = first["dispatched"][0]
            brief_path = (
                control.config.coordination_root
                / "tasks"
                / dispatched["dispatch_task_id"]
                / "brief.md"
            )
            self.assertEqual(first["claimed"], 1)
            self.assertEqual(second["claimed"], 0)
            self.assertEqual(
                brief_path.read_text(encoding="utf-8"),
                "Implement only the schema change.\n",
            )
            self.assertEqual(first["snapshot"]["running"][0]["job_id"], dispatched["job_id"])

    def test_plan_dispatch_uses_the_prepared_slot_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_path = _git_repo(root / "repo", "main")
            slot_path = _git_repo(root / "slots" / "main-1", "codex/autonomous-supervisor")
            control = AgentControlPlane(_config_with_slot(root, route_path, slot_path))
            control.create_plan(
                plan_id="slot-dispatch",
                title="Slot dispatch",
                tasks=(
                    PlanTaskDefinition(
                        "supervisor",
                        "Supervisor",
                        execution=PlanExecutionSpec(
                            route="main",
                            slot="main-1",
                            brief="Implement the supervisor service.",
                            backend=CODEX_BACKEND,
                            workspace_access="native",
                        ),
                    ),
                ),
            )

            with patch.object(control, "_launch_worker", return_value=123):
                dispatched = control.dispatch_plan("slot-dispatch", max_jobs=1)

            self.assertEqual(dispatched["failures"], [])
            job_id = dispatched["dispatched"][0]["job_id"]
            self.assertEqual(
                control.store.get_job(job_id).expected_branch,
                "codex/autonomous-supervisor",
            )

    def test_plan_dispatch_preserves_explicit_spark_model_and_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            config = _config(root, workspace)
            config = replace(
                config,
                defaults=replace(
                    config.defaults,
                    codex_model="gpt-5.6-terra",
                    codex_reasoning_effort="low",
                ),
            )
            control = AgentControlPlane(config)
            _brief(control.config.coordination_root, "schema")
            control.create_plan(
                plan_id="dispatch-spark",
                title="Dispatch Spark",
                tasks=(
                    PlanTaskDefinition(
                        "schema",
                        "Schema",
                        execution=PlanExecutionSpec(
                            route="main",
                            brief="Run with Spark profile.",
                            backend=CODEX_BACKEND,
                            codex_model="gpt-5.3-codex-spark",
                            codex_reasoning_effort="high",
                        ),
                    ),
                ),
            )

            with patch.object(control, "_launch_worker", return_value=123):
                dispatched = control.dispatch_plan("dispatch-spark", max_jobs=1)

            job_id = dispatched["dispatched"][0]["job_id"]
            job = control.store.get_job(job_id)
            summary = control.summary_job(job_id)
            snapshot = control.plan_snapshot("dispatch-spark")

            self.assertEqual(job.codex_model, "gpt-5.3-codex-spark")
            self.assertEqual(job.codex_reasoning_effort, "high")
            self.assertIsNone(job.codex_quality_tier)
            self.assertEqual(summary["codex_quota_domain"], "spark")
            self.assertEqual(
                snapshot["running"][0]["execution"]["codex_model"],
                "gpt-5.3-codex-spark",
            )
            worker_control = AgentControlPlane(config)
            worker_job = worker_control.store.get_job(job_id)
            worker_ladder = worker_control.job_execution._model_ladder_for_job(worker_job)
            self.assertEqual(
                worker_ladder,
                (ModelProfile("gpt-5.3-codex-spark", "high"),),
            )

    def test_named_policy_validates_cached_candidates_and_uses_its_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            base_config = _config(root, workspace)
            base_config.model_catalog.cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "invented-cached-model",
                                "supported_reasoning_levels": ["low", "ultra"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            policy = CodexRoutingPolicyConfig(
                name="implementation-fast-path",
                task_class="implementation",
                tool_call_budget=77,
                candidates=(CodexRoutingCandidateConfig("invented-cached-model", "ultra"),),
            )
            defaults = replace(
                base_config.defaults,
                codex_quality_tier="implementation-fast-path",
            )

            with self.assertRaisesRegex(ValueError, "not a visible candidate"):
                AgentControlPlane(
                    replace(
                        base_config,
                        defaults=defaults,
                        routing_policies=(
                            replace(
                                policy,
                                candidates=(CodexRoutingCandidateConfig("missing-model", "ultra"),),
                            ),
                        ),
                    )
                )

            config = replace(base_config, defaults=defaults, routing_policies=(policy,))
            control = AgentControlPlane(config)
            _brief(config.coordination_root, "named-policy")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(StartOptions(task_id="named-policy", route="main"))

            self.assertEqual(job.codex_quality_tier, "implementation-fast-path")
            self.assertEqual(job.codex_model, "invented-cached-model")
            self.assertEqual(job.codex_reasoning_effort, "ultra")
            self.assertEqual(job.codex_tool_call_budget, 77)

    def test_model_routing_explain_returns_adaptive_decision_and_calls_policy_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = _adaptive_control(root, workspace)
            history = [
                _routing_history_row(control),
                _routing_history_row(control, duration_sec=40),
            ]

            with (
                patch.object(
                    control.store, "routing_history", return_value=history
                ) as read_history,
                patch.object(
                    control.model_routing,
                    "decision_for_policy",
                    wraps=control.model_routing.decision_for_policy,
                ) as decide,
            ):
                payload = control.model_routing_explain("adaptive", "main")

            read_history.assert_called_once_with(limit=200)
            decide.assert_called_once()
            self.assertEqual(payload["route"], "main")
            self.assertEqual(payload["policy"], "adaptive")
            self.assertEqual(payload["task_class"], "implementation")
            self.assertEqual(payload["chosen_model"], "gpt-5.6-luna")
            self.assertEqual(payload["chosen_reasoning_effort"], "low")
            self.assertEqual(payload["tool_call_budget"], 77)
            self.assertEqual(payload["selection_source"], "history")
            self.assertIsNone(payload["fallback_reason"])
            self.assertEqual(payload["candidate_scores"][0]["sample_count"], 2)
            self.assertEqual(payload["candidate_scores"][0]["expected_duration_sec"], 35.0)
            self.assertEqual(payload["catalog"]["source"], control.model_catalog.source)

    def test_model_routing_explain_shows_configured_fallback_for_zero_and_one_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = _adaptive_control(root, workspace)

            for history in ([], [_routing_history_row(control)]):
                with (
                    self.subTest(sample_count=len(history)),
                    patch.object(control.store, "routing_history", return_value=history),
                ):
                    payload = control.model_routing_explain("adaptive", "main")

                self.assertTrue(payload["configured_fallback"])
                self.assertEqual(payload["selection_source"], "configured_fallback")
                self.assertEqual(
                    payload["fallback_reason"],
                    "insufficient comparative samples for every candidate",
                )
                self.assertEqual(payload["candidate_scores"][0]["sample_count"], len(history))

    def test_model_routing_explain_ignores_malformed_history_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = _adaptive_control(root, workspace)
            malformed = {"model": "gpt-5.6-luna", "reasoning_effort": "low", "input_tokens": "bad"}
            valid = _routing_history_row(control)

            with patch.object(control.store, "routing_history", return_value=[malformed, valid]):
                payload = control.model_routing_explain("adaptive", "main")

            self.assertEqual(payload["candidate_scores"][0]["sample_count"], 1)
            self.assertTrue(payload["configured_fallback"])
            self.assertEqual(
                payload["fallback_reason"],
                "insufficient comparative samples for every candidate",
            )

    def test_explain_and_launcher_share_the_same_strict_history_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_adaptive_config(root, workspace))
            _brief(control.config.coordination_root, "adaptive-parity")
            malformed = _routing_history_mapping(control, "gpt-5.6-luna")
            del malformed["metrics_valid"]
            history = [
                malformed,
                _routing_history_mapping(control, "gpt-5.6-luna"),
                _routing_history_mapping(control, "gpt-5.6-luna"),
                {
                    **_routing_history_mapping(control, "gpt-5.6-terra"),
                    "duration_sec": 2.0,
                },
                {
                    **_routing_history_mapping(control, "gpt-5.6-terra"),
                    "duration_sec": 2.0,
                },
            ]

            with patch.object(control.store, "routing_history", return_value=history):
                explained = control.model_routing_explain("adaptive-routing", "main")
                with patch.object(control, "_launch_worker", return_value=123):
                    job = control.start_job(
                        StartOptions(
                            task_id="adaptive-parity",
                            route="main",
                            backend=CODEX_BACKEND,
                        )
                    )

            persisted = control.store.routing_decision(job.job_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(explained["selection_source"], persisted["selection_source"])
            self.assertEqual(explained["chosen_model"], job.codex_model)
            self.assertEqual(explained["ladder"], persisted["ladder"])
            self.assertEqual(explained["candidate_scores"][0]["sample_count"], 2)

    def test_mechanical_quality_tier_starts_on_luna_without_changing_deep_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            config = _config(root, workspace)
            config = replace(
                config,
                defaults=replace(config.defaults, codex_mechanical_tool_call_budget=57),
            )
            control = AgentControlPlane(config)
            _brief(control.config.coordination_root, "task-mechanical")

            with patch.object(control, "_launch_worker", return_value=123):
                job = control.start_job(
                    StartOptions(
                        task_id="task-mechanical",
                        route="main",
                        backend=CODEX_BACKEND,
                        codex_quality_tier="mechanical",
                    )
                )

            self.assertEqual(job.codex_quality_tier, "mechanical")
            self.assertEqual(job.codex_model, "gpt-5.6-luna")
            self.assertEqual(job.codex_reasoning_effort, "low")
            self.assertEqual(job.codex_tool_call_budget, 57)
            self.assertEqual(control.config.defaults.codex_quality_tier, "deep")
            self.assertEqual(control.model_routing.policy_names, ("mechanical", "balanced", "deep"))

    def test_explicit_premium_model_requires_override_reason_and_persists_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "premium-missing")
            _brief(control.config.coordination_root, "premium-explicit")

            with patch.object(control, "_launch_worker", return_value=123):
                with self.assertRaisesRegex(PolicyError, "nonblank"):
                    control.start_job(
                        StartOptions(
                            task_id="premium-missing",
                            route="main",
                            backend=CODEX_BACKEND,
                            codex_model="gpt-5.6-sol",
                            codex_reasoning_effort="medium",
                        )
                    )
                job = control.start_job(
                    StartOptions(
                        task_id="premium-explicit",
                        route="main",
                        backend=CODEX_BACKEND,
                        codex_model="gpt-5.6-sol",
                        codex_reasoning_effort="medium",
                        codex_premium_override_reason="approved benchmark run",
                    )
                )

            self.assertEqual(job.codex_premium_override_reason, "approved benchmark run")
            self.assertEqual(
                control.status_job(job.job_id)["codex_premium_override_reason"],
                "approved benchmark run",
            )
            self.assertEqual(
                control.summary_job(job.job_id)["codex_premium_override_reason"],
                "approved benchmark run",
            )

    def test_explicit_profile_cannot_combine_with_quality_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "profile-policy-conflict")

            with self.assertRaisesRegex(PolicyError, "either automatic policy routing"):
                control.start_job(
                    StartOptions(
                        task_id="profile-policy-conflict",
                        route="main",
                        backend=CODEX_BACKEND,
                        codex_model="gpt-5.6-luna",
                        codex_reasoning_effort="low",
                        codex_quality_tier="mechanical",
                    )
                )

    def test_capacity_does_not_escalate_mechanical_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-capacity",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.6-luna",
                codex_reasoning_effort="low",
                codex_quality_tier="mechanical",
                codex_tool_call_budget=45,
            )
            control.store.record_routing_decision(
                job.job_id,
                {
                    "event": "routing_decision",
                    "route": "main",
                    "requested_policy": "mechanical",
                    "task_class": "mechanical",
                    "tool_call_budget": 45,
                    "catalog": {
                        "source": control.model_routing.catalog.source,
                        "version": control.model_routing.catalog.version,
                    },
                    "selection_source": "configured_fallback",
                    "configured_fallback": True,
                    "chosen_profile": {
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "low",
                    },
                    "ladder": [
                        {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
                        {"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
                    ],
                },
            )
            runner = _CapacityThenCompletedRunner()
            broker = _RecordingQuotaBroker()
            control.quota_broker = broker  # type: ignore[assignment]

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=runner,
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "completed")
            self.assertEqual(finished.codex_model, "gpt-5.6-luna")
            self.assertEqual(finished.codex_reasoning_effort, "low")
            self.assertEqual(
                [spec.codex_model for spec in runner.specs],
                ["gpt-5.6-luna", "gpt-5.6-luna"],
            )
            self.assertIsNone(runner.specs[0].codex_resume_thread_id)
            self.assertEqual(broker.capacity_units, [2])
            self.assertEqual(broker.released_jobs, [job.job_id])

    def test_model_capability_partial_escalates_and_resizes_quota_on_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-capability-escalation",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.6-luna",
                codex_reasoning_effort="low",
                codex_quality_tier="mechanical",
                codex_tool_call_budget=45,
            )
            _record_mechanical_ladder(control, job.job_id)
            runner = _ClassifiedPartialThenCompletedRunner("model_capability")
            broker = _RecordingQuotaBroker()
            control.quota_broker = broker  # type: ignore[assignment]

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=runner,
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "completed")
            self.assertEqual(
                [spec.codex_model for spec in runner.specs], ["gpt-5.6-luna", "gpt-5.6-terra"]
            )
            self.assertEqual(
                [spec.codex_resume_thread_id for spec in runner.specs], [None, "thread-classified"]
            )
            self.assertEqual(broker.capacity_units, [2, 10])
            self.assertTrue(
                any(
                    "Model escalation accepted; classification=model_capability" in event[2]
                    for event in control.store.recent_events(job.job_id)
                )
            )

    def test_infrastructure_partial_stays_on_luna_and_refuses_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-infrastructure-partial",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.6-luna",
                codex_reasoning_effort="low",
                codex_quality_tier="mechanical",
                codex_tool_call_budget=45,
            )
            _record_mechanical_ladder(control, job.job_id)
            runner = _ClassifiedPartialThenCompletedRunner("infrastructure")

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=runner,
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "completed")
            self.assertEqual(
                [spec.codex_model for spec in runner.specs], ["gpt-5.6-luna", "gpt-5.6-luna"]
            )
            self.assertEqual(
                [spec.codex_resume_thread_id for spec in runner.specs], [None, "thread-classified"]
            )
            self.assertTrue(
                any(
                    "Model escalation refused; classification=infrastructure" in event[2]
                    for event in control.store.recent_events(job.job_id)
                )
            )

    def test_malformed_partial_stays_on_luna_as_unclassified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-malformed-partial",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.6-luna",
                codex_reasoning_effort="low",
                codex_quality_tier="mechanical",
                codex_tool_call_budget=45,
            )
            _record_mechanical_ladder(control, job.job_id)
            runner = _ClassifiedPartialThenCompletedRunner("not-a-classification")

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=runner,
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "completed")
            self.assertEqual(
                [spec.codex_model for spec in runner.specs], ["gpt-5.6-luna", "gpt-5.6-luna"]
            )
            self.assertTrue(
                any(
                    "Model escalation refused; classification=unclassified" in event[2]
                    for event in control.store.recent_events(job.job_id)
                )
            )

    def test_partial_deep_job_continues_same_model_on_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-partial",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.6-terra",
                codex_reasoning_effort="medium",
                codex_quality_tier="deep",
            )
            runner = _PartialThenCompletedRunner()

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=runner,
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "completed")
            self.assertEqual(
                [spec.codex_model for spec in runner.specs],
                ["gpt-5.6-terra", "gpt-5.6-terra"],
            )
            self.assertIsNone(runner.specs[0].codex_resume_thread_id)
            self.assertEqual(runner.specs[1].codex_resume_thread_id, "thread-partial")
            self.assertIn("Continue the same assigned task", runner.specs[1].prompt)

    def test_quota_wait_retries_without_starting_model_until_acquired(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-quota",
                backend=CODEX_BACKEND,
            )
            broker = _SequenceQuotaBroker()
            control.quota_broker = broker  # type: ignore[assignment]

            with patch("agent_control_plane.app.runtime.job_execution_service.time.sleep"):
                acquired = control.job_execution.wait_for_codex_quota(job)

            self.assertTrue(acquired)
            self.assertEqual(broker.calls, 2)
            self.assertEqual(broker.capacity_units, [30, 30])
            self.assertEqual(control.store.get_job(job.job_id).status, "running")

    def test_codex_quota_domain_appears_in_status_summary_and_watch_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            _create_job(
                control,
                root,
                workspace,
                "job-quota-domain",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.3-codex-spark",
            )

            status = control.status_job("job-quota-domain")
            summary = control.summary_job("job-quota-domain")
            watch = control.watch_job(
                "job-quota-domain",
                include_details=False,
                poll_interval_sec=0,
                timeout_sec=0,
            )

            self.assertEqual(status["codex_quota_domain"], "spark")
            self.assertEqual(summary["codex_quota_domain"], "spark")
            self.assertEqual(watch["codex_quota_domain"], "spark")

    def test_quota_wait_records_domain_in_event_and_passes_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-quota-domain",
                backend=CODEX_BACKEND,
                codex_model="gpt-5.3-codex-spark",
            )
            broker = _SequenceQuotaBroker()
            control.quota_broker = broker  # type: ignore[assignment]

            with patch("agent_control_plane.app.runtime.job_execution_service.time.sleep"):
                acquired = control.job_execution.wait_for_codex_quota(job)

            self.assertTrue(acquired)
            self.assertEqual(broker.models, ["gpt-5.3-codex-spark", "gpt-5.3-codex-spark"])
            events = control.store.recent_events(job.job_id)
            self.assertTrue(any("domain=spark" in event[2] for event in events))

    def test_read_only_slot_job_skips_ide_and_dependency_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_path = _git_repo(root / "repo", "main")
            slot_path = _git_repo(root / "slots" / "main-1", "main")
            control = AgentControlPlane(_config_with_slot(root, route_path, slot_path))
            _brief(control.config.coordination_root, "task-read-only")

            with (
                patch.object(control.slots, "ensure_ide_root_module") as ensure_ide_root,
                patch.object(control.slots, "prepare_slot") as prepare_slot,
                patch.object(control, "_launch_worker", return_value=123),
            ):
                job = control.start_job(
                    StartOptions(
                        task_id="task-read-only",
                        route="main",
                        slot="main-1",
                        read_only=True,
                    )
                )

            self.assertEqual(job.status, "queued")
            ensure_ide_root.assert_not_called()
            prepare_slot.assert_not_called()

    def test_blocked_start_does_not_prepare_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_path = _git_repo(root / "repo", "main")
            slot_path = _git_repo(root / "slots" / "main-1", "main")
            control = AgentControlPlane(_config_with_slot(root, route_path, slot_path))

            with (
                patch.object(control.slots, "ensure_ide_root_module") as ensure_ide_root,
                patch.object(control.slots, "prepare_slot") as prepare_slot,
                self.assertRaisesRegex(PolicyError, "Task brief not found"),
            ):
                control.start_job(
                    StartOptions(
                        task_id="missing-brief",
                        route="main",
                        slot="main-1",
                    )
                )

            ensure_ide_root.assert_not_called()
            prepare_slot.assert_not_called()

    def test_reusing_task_id_does_not_overwrite_coordination_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-duplicate")
            result_path = (
                control.config.coordination_root / "tasks" / "task-duplicate" / "result.md"
            )

            with patch.object(control, "_launch_worker", return_value=123):
                control.start_job(StartOptions(task_id="task-duplicate", route="main"))
                result_path.write_text("sentinel\n", encoding="utf-8")

                with self.assertRaisesRegex(PolicyError, "Task ID already exists"):
                    control.start_job(StartOptions(task_id="task-duplicate", route="main"))

            self.assertEqual(result_path.read_text(encoding="utf-8"), "sentinel\n")

    def test_blocked_runner_result_finishes_job_as_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1")

            with patch(
                "agent_control_plane.app.runtime.orchestrator.PtyAgyRunner",
                return_value=_BlockedRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "blocked")
            self.assertIn("workspace trust prompt", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("workspace trust prompt", result_text)

    def test_guardrail_detects_changes_to_preexisting_forbidden_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            (workspace / "uv.lock").write_text("before\n", encoding="utf-8")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1", allow_dirty=True)

            with patch(
                "agent_control_plane.app.runtime.orchestrator.PtyAgyRunner",
                return_value=_MutatingForbiddenFileRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("uv.lock", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("uv.lock", result_text)

    def test_codex_dirty_diff_guardrail_stops_large_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            tracked_file = workspace / "tracked.py"
            tracked_file.write_text("base\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], workspace)
            _run(
                [
                    "git",
                    "-c",
                    "user.name=Agy Test",
                    "-c",
                    "user.email=agy@example.test",
                    "commit",
                    "-m",
                    "seed",
                ],
                workspace,
            )
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-1",
                backend=CODEX_BACKEND,
            )

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=_LargeDirtyCodexRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("Codex dirty diff exceeded", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("tracked.py", result_text)
            self.assertIn("guardrail.patch", result_text)
            self.assertTrue((job.run_dir / "guardrail.patch").exists())

    def test_codex_dirty_diff_guardrail_limits_growth_for_resumed_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            tracked_file = workspace / "tracked.py"
            tracked_file.write_text("base\n", encoding="utf-8")
            _run(["git", "add", "tracked.py"], workspace)
            _run(
                [
                    "git",
                    "-c",
                    "user.name=Agy Test",
                    "-c",
                    "user.email=agy@example.test",
                    "commit",
                    "-m",
                    "seed",
                ],
                workspace,
            )
            tracked_file.write_text(
                "".join(f"baseline {index}\n" for index in range(10)),
                encoding="utf-8",
            )
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                workspace,
                "job-1",
                backend=CODEX_BACKEND,
                allow_dirty=True,
            )

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=_LargeDirtyCodexRunner(),
            ):
                finished = control.run_job(job.job_id)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("Codex dirty diff exceeded", finished.last_error or "")
            self.assertIn("baseline", finished.last_error or "")
            self.assertIn("growth", finished.last_error or "")

    def test_slot_job_guardrail_detects_route_root_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            route_root = _git_repo(root / "repo", "main")
            slot = _git_repo(root / "worktrees" / "slot-1", "main")
            control = AgentControlPlane(_config(root, route_root))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(
                control,
                root,
                slot,
                "job-1",
                backend=CODEX_BACKEND,
                slot_name="dev-1",
            )

            with patch(
                "agent_control_plane.app.runtime.orchestrator.CodexExecRunner",
                return_value=_MutatingRouteRootRunner(route_root),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "guardrail_violation")
            self.assertIn("route root outside assigned workspace", finished.last_error or "")
            self.assertIn("wrong-root.py", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("wrong-root.py", result_text)
            self.assertTrue((job.run_dir / "route-root-guardrail-status.txt").exists())
            self.assertTrue((job.run_dir / "route-root-guardrail.patch").exists())

    def test_failed_runner_attempts_write_blocked_result_before_finishing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1")

            with patch(
                "agent_control_plane.app.runtime.orchestrator.PtyAgyRunner",
                return_value=_TimeoutRunner(),
            ):
                finished = control.run_job(job.job_id)

            result_text = job.result_path.read_text(encoding="utf-8")
            result_state = inspect_result(job.result_path, 0.0)

            self.assertEqual(finished.status, "failed")
            self.assertIn("No progress before timeout", finished.last_error or "")
            self.assertEqual(result_state.status, "blocked")
            self.assertIn("No progress before timeout", result_text)

    def test_slot_release_status_preserves_dirty_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = _git_repo(root / "repo", "main")
            control = AgentControlPlane(_config(root, workspace))
            _brief(control.config.coordination_root, "task-1")
            job = _create_job(control, root, workspace, "job-1")
            (workspace / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            status, note = control.finalization._slot_release_status(job, "cancelled")

            self.assertEqual(status, "dirty_after_job")
            self.assertIn("finished cancelled with dirty workspace", note or "")
            self.assertIn("dirty.txt", note or "")


class _CapacityThenCompletedRunner:
    def __init__(self) -> None:
        self.specs: list[Any] = []

    def run(self, spec: Any, **_kwargs: Any) -> AgyRunResult:
        self.specs.append(spec)
        if len(self.specs) == 1:
            return AgyRunResult(
                status="capacity",
                completed=False,
                exit_code=1,
                result_status=None,
                message="usage limit reached",
                metrics=_attempt_metrics(thread_id="thread-capacity"),
            )
        spec.result_path.parent.mkdir(parents=True, exist_ok=True)
        spec.result_path.write_text("Status: completed\n", encoding="utf-8")
        return AgyRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="completed after escalation",
            metrics=_attempt_metrics(thread_id="thread-capacity"),
        )


class _PartialThenCompletedRunner:
    def __init__(self) -> None:
        self.specs: list[Any] = []

    def run(self, spec: Any, **_kwargs: Any) -> AgyRunResult:
        self.specs.append(spec)
        spec.result_path.parent.mkdir(parents=True, exist_ok=True)
        if len(self.specs) == 1:
            spec.result_path.write_text("Status: partial\n", encoding="utf-8")
            return AgyRunResult(
                status="completed",
                completed=True,
                exit_code=1,
                result_status="partial",
                message="partial checkpoint",
                metrics=_attempt_metrics(thread_id="thread-partial"),
            )
        spec.result_path.write_text("Status: completed\n", encoding="utf-8")
        return AgyRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="completed after continuation",
            metrics=_attempt_metrics(thread_id="thread-partial"),
        )


class _ClassifiedPartialThenCompletedRunner:
    def __init__(self, classification: str) -> None:
        self.classification = classification
        self.specs: list[Any] = []

    def run(self, spec: Any, **_kwargs: Any) -> AgyRunResult:
        self.specs.append(spec)
        spec.result_path.parent.mkdir(parents=True, exist_ok=True)
        if len(self.specs) == 1:
            spec.result_path.write_text(
                f"Status: partial\nEscalation-Classification: {self.classification}\n",
                encoding="utf-8",
            )
            return AgyRunResult(
                status="completed",
                completed=True,
                exit_code=1,
                result_status="partial",
                message="classified partial checkpoint",
                metrics=_attempt_metrics(thread_id="thread-classified"),
                escalation_classification=inspect_result(
                    spec.result_path, 0.0
                ).escalation_classification,
            )
        spec.result_path.write_text("Status: completed\n", encoding="utf-8")
        return AgyRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="completed after classified continuation",
            metrics=_attempt_metrics(thread_id="thread-classified"),
        )


class _RecordingQuotaBroker:
    def __init__(self) -> None:
        self.capacity_units: list[int] = []
        self.released_jobs: list[str] = []
        self.models: list[str | None] = []

    def try_acquire(
        self,
        _job_id: str,
        *,
        worker_pid: int,
        capacity_units: int,
        model: str | None = None,
    ) -> QuotaDecision:
        self.capacity_units.append(capacity_units)
        self.models.append(model)
        quota_domain = "spark" if model == "gpt-5.3-codex-spark" else "primary"
        return QuotaDecision(
            acquired=True,
            reason=None,
            active_jobs=1,
            active_capacity_units=capacity_units,
            max_capacity_units=60,
            quota_domain=quota_domain,
        )

    def release(self, job_id: str) -> None:
        self.released_jobs.append(job_id)


class _SequenceQuotaBroker:
    def __init__(self) -> None:
        self.calls = 0
        self.capacity_units: list[int] = []
        self.models: list[str | None] = []

    def try_acquire(
        self,
        _job_id: str,
        *,
        worker_pid: int,
        capacity_units: int,
        model: str | None = None,
    ) -> QuotaDecision:
        self.calls += 1
        self.capacity_units.append(capacity_units)
        self.models.append(model)
        quota_domain = "spark" if model == "gpt-5.3-codex-spark" else "primary"
        if self.calls == 1:
            return QuotaDecision(
                acquired=False,
                reason="weighted_capacity_limit",
                active_jobs=2,
                active_capacity_units=60,
                max_capacity_units=60,
                quota_domain=quota_domain,
            )
        return QuotaDecision(
            acquired=True,
            reason=None,
            active_jobs=1,
            active_capacity_units=capacity_units,
            max_capacity_units=60,
            quota_domain=quota_domain,
        )


def _attempt_metrics(*, thread_id: str) -> AttemptMetrics:
    return AttemptMetrics(
        duration_sec=1.0,
        thread_id=thread_id,
        event_count=1,
        turn_completed=False,
        usage_available=False,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        tool_calls=0,
        failed_tool_calls=0,
        error_events=0,
        tool_counts=(),
        estimated_credits=None,
        estimated_api_usd=None,
        rate_card_version="test",
        event_log_path=None,
    )


class _BlockedRunner:
    def run(self, *args: Any, **kwargs: Any) -> AgyRunResult:
        return AgyRunResult(
            status="blocked",
            completed=False,
            exit_code=None,
            result_status=None,
            message="Antigravity CLI is waiting for the workspace trust prompt.",
        )


class _CapturingCompletedRunner:
    def __init__(self) -> None:
        self.specs: list[Any] = []

    def run(self, spec: Any, **_kwargs: Any) -> AgyRunResult:
        self.specs.append(spec)
        spec.result_path.parent.mkdir(parents=True, exist_ok=True)
        spec.result_path.write_text("Status: completed\n", encoding="utf-8")
        return AgyRunResult(
            status="completed",
            completed=True,
            exit_code=0,
            result_status="completed",
            message="completed",
            metrics=_attempt_metrics(thread_id="thread-native"),
        )


class _MutatingForbiddenFileRunner:
    def run(self, spec: Any, **kwargs: Any) -> AgyRunResult:
        (spec.workspace_path / "uv.lock").write_text("after\n", encoding="utf-8")
        kwargs["cancel_requested"]()
        return AgyRunResult(
            status="cancelled",
            completed=False,
            exit_code=None,
            result_status=None,
            message="runner stopped after guardrail check",
        )


class _LargeDirtyCodexRunner:
    def run(self, spec: Any, **kwargs: Any) -> AgyRunResult:
        changed_lines = "".join(f"changed {index}\n" for index in range(520))
        (spec.workspace_path / "tracked.py").write_text(changed_lines, encoding="utf-8")
        kwargs["cancel_requested"]()
        return AgyRunResult(
            status="cancelled",
            completed=False,
            exit_code=None,
            result_status=None,
            message="runner stopped after Codex dirty diff guardrail check",
        )


class _MutatingRouteRootRunner:
    def __init__(self, route_root: Path) -> None:
        self._route_root = route_root

    def run(self, _spec: Any, **kwargs: Any) -> AgyRunResult:
        (self._route_root / "wrong-root.py").write_text("wrong\n", encoding="utf-8")
        kwargs["cancel_requested"]()
        return AgyRunResult(
            status="cancelled",
            completed=False,
            exit_code=None,
            result_status=None,
            message="runner stopped after route-root guardrail check",
        )


class _TimeoutRunner:
    def run(self, *args: Any, **kwargs: Any) -> AgyRunResult:
        return AgyRunResult(
            status="timeout",
            completed=False,
            exit_code=None,
            result_status=None,
            message="No progress before timeout",
        )


def _git_repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], path)
    _run(["git", "checkout", "-b", branch], path)
    return path


def _run(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise unittest.SkipTest("git is not installed") from exc


def _brief(coordination_root: Path, task_id: str) -> None:
    task_dir = coordination_root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (coordination_root / "agent-protocol.md").write_text("# Protocol\n", encoding="utf-8")
    (coordination_root / "workspace-routing.md").write_text("# Routing\n", encoding="utf-8")
    (task_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")


def _adaptive_control(root: Path, workspace: Path) -> AgentControlPlane:
    policy = CodexRoutingPolicyConfig(
        name="adaptive",
        task_class="implementation",
        tool_call_budget=77,
        candidates=(CodexRoutingCandidateConfig("gpt-5.6-luna", "low"),),
        adaptive=CodexAdaptiveRoutingConfig(
            minimum_samples_per_candidate=2,
            history_window=20,
            quality_floor=0.5,
            prior_quality=0.5,
            prior_weight=1.0,
            allow_missing_price=True,
        ),
    )
    config = _config(root, workspace)
    config = replace(
        config,
        defaults=replace(config.defaults, codex_quality_tier="adaptive"),
        routing_policies=(policy,),
    )
    return AgentControlPlane(config)


def _routing_history_row(
    control: AgentControlPlane,
    *,
    duration_sec: float = 30.0,
) -> dict[str, Any]:
    return {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "attempt_status": "completed",
        "result_status": "completed",
        "input_tokens": 1_000,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "duration_sec": duration_sec,
        "metrics_valid": True,
        "route": "main",
        "policy_name": "adaptive",
        "task_class": "implementation",
        "selection_source": "configured_fallback",
        "catalog_source": control.model_catalog.source,
        "catalog_version": control.model_catalog.version,
        "root_outcome": "accepted",
        "defects_found": 0,
    }


def _create_job(
    control: AgentControlPlane,
    root: Path,
    workspace: Path,
    job_id: str,
    *,
    allow_dirty: bool = False,
    backend: str = AGY_BACKEND,
    slot_name: str | None = None,
    codex_model: str | None = None,
    codex_reasoning_effort: str | None = None,
    codex_quality_tier: str | None = None,
    codex_tool_call_budget: int | None = None,
    workspace_access: str = "ide_mcp",
):
    run_dir = root / "runs" / job_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text("Do work and write result.md", encoding="utf-8")
    return control.store.create_job(
        job_id=job_id,
        task_id="task-1",
        route="main",
        workspace_path=workspace,
        expected_branch="main",
        config_path=root / "workspaces.toml",
        run_dir=run_dir,
        prompt_path=prompt_path,
        result_path=root / ".agent-work" / "tasks" / "task-1" / "result.md",
        timeout_sec=10,
        idle_timeout_sec=5,
        print_timeout="10s",
        max_restarts=1,
        yolo=False,
        allow_dirty=allow_dirty,
        read_only=False,
        backend=backend,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
        codex_quality_tier=codex_quality_tier,
        codex_tool_call_budget=codex_tool_call_budget,
        workspace_access=workspace_access,
        slot_name=slot_name,
    )


def _record_mechanical_ladder(control: AgentControlPlane, job_id: str) -> None:
    control.store.record_routing_decision(
        job_id,
        {
            "event": "routing_decision",
            "route": "main",
            "requested_policy": "mechanical",
            "task_class": "mechanical",
            "tool_call_budget": 45,
            "catalog": {
                "source": control.model_routing.catalog.source,
                "version": control.model_routing.catalog.version,
            },
            "selection_source": "configured_fallback",
            "configured_fallback": True,
            "chosen_profile": {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
            "ladder": [
                {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
                {"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
            ],
        },
    )


def _config(root: Path, route_path: Path) -> ControlConfig:
    model_catalog = _model_catalog(root)
    return ControlConfig(
        config_path=root / "workspaces.toml",
        project_root=root,
        coordination_root=root / ".agent-work",
        runs_root=root / "runs",
        database_path=root / "runs" / "jobs.sqlite3",
        worktree_root=root / "worktrees",
        worktree_base=route_path,
        slot_root=root / "slots",
        agy_command="agy",
        codex_command="codex",
        defaults=ControlDefaults(
            timeout_sec=10,
            idle_timeout_sec=5,
            print_timeout="10s",
            max_restarts=1,
            yolo=False,
            allow_dirty=False,
            prepare_slots=False,
            guardrail_poll_sec=2.0,
            forbidden_status_globs=("uv.lock", ".venv/**"),
            codex_model="gpt-5.6-terra",
            codex_mechanical_model="gpt-5.6-luna",
            codex_balanced_model="gpt-5.6-terra",
            codex_deep_model="gpt-5.6-terra",
        ),
        model_catalog=model_catalog,
        routes=MappingProxyType(
            {
                "main": RouteConfig(
                    name="main",
                    path=route_path,
                    required_branch="main",
                    worktree_root=root / "worktrees",
                    worktree_base=route_path,
                    source_roots=(Path("src"),),
                    test_roots=(Path("tests"),),
                    exclude_dirs=(),
                )
            }
        ),
        slots=MappingProxyType({}),
        slot_prepare=(),
    )


def _adaptive_config(root: Path, route_path: Path) -> ControlConfig:
    base = _config(root, route_path)
    policy = CodexRoutingPolicyConfig(
        name="adaptive-routing",
        task_class="implementation",
        tool_call_budget=91,
        candidates=(
            CodexRoutingCandidateConfig("gpt-5.6-terra", "low"),
            CodexRoutingCandidateConfig("gpt-5.6-luna", "low"),
        ),
        adaptive=CodexAdaptiveRoutingConfig(
            minimum_samples_per_candidate=2,
            history_window=20,
            quality_floor=0.8,
            prior_quality=0.75,
            prior_weight=2.0,
            allow_missing_price=True,
        ),
    )
    return replace(
        base,
        defaults=replace(base.defaults, codex_quality_tier=policy.name),
        routing_policies=(policy,),
    )


def _routing_history_mapping(
    control: AgentControlPlane,
    model: str,
) -> dict[str, object]:
    return {
        "model": model,
        "reasoning_effort": "low",
        "attempt_status": "completed",
        "result_status": "completed",
        "input_tokens": 1_000,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "duration_sec": 1.0,
        "root_outcome": "accepted",
        "defects_found": 0,
        "catalog_source": control.model_routing.catalog.source,
        "catalog_version": control.model_routing.catalog.version,
        "metrics_valid": True,
        "route": "main",
        "policy_name": "adaptive-routing",
        "task_class": "implementation",
        "selection_source": "configured_fallback",
    }


def _model_catalog(root: Path) -> CodexModelCatalogConfig:
    cache_path = root / "models_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "supported_reasoning_levels": [
                            "none",
                            "low",
                            "medium",
                            "high",
                            "xhigh",
                        ],
                    },
                    {
                        "slug": "gpt-5.6-terra",
                        "supported_reasoning_levels": [
                            "none",
                            "low",
                            "medium",
                            "high",
                            "xhigh",
                        ],
                    },
                    {
                        "slug": "gpt-5.3-codex-spark",
                        "supported_reasoning_levels": ["low", "high"],
                    },
                    {
                        "slug": "gpt-5.6-sol",
                        "supported_reasoning_levels": ["medium", "high"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return CodexModelCatalogConfig(
        cache_path=cache_path,
        max_cache_age_sec=60.0,
        models=(
            CodexModelMetadataConfig(
                model="gpt-5.6-luna",
                quota_domain=None,
                capacity_units=(("low", 2),),
                credit_rate=None,
                api_usd_rate=None,
                rate_card_version=None,
                rate_card_source=None,
            ),
            CodexModelMetadataConfig(
                model="gpt-5.6-terra",
                quota_domain=None,
                capacity_units=(("medium", 10),),
                credit_rate=None,
                api_usd_rate=None,
                rate_card_version=None,
                rate_card_source=None,
            ),
            CodexModelMetadataConfig(
                model="gpt-5.3-codex-spark",
                quota_domain="spark",
                capacity_units=(),
                credit_rate=None,
                api_usd_rate=None,
                rate_card_version=None,
                rate_card_source=None,
            ),
            CodexModelMetadataConfig(
                model="gpt-5.6-sol",
                premium=True,
                quota_domain="primary",
                capacity_units=(("medium", 20),),
                credit_rate=None,
                api_usd_rate=None,
                rate_card_version=None,
                rate_card_source=None,
            ),
        ),
        quota_domains=(
            CodexQuotaDomainConfig("primary", 2, 8, 75.0),
            CodexQuotaDomainConfig("spark", 8, 32, 100.0),
        ),
    )


def _config_with_slot(root: Path, route_path: Path, slot_path: Path) -> ControlConfig:
    config = _config(root, route_path)
    return replace(
        config,
        defaults=replace(config.defaults, prepare_slots=True),
        slots=MappingProxyType(
            {
                "main-1": SlotConfig(
                    name="main-1",
                    route="main",
                    path=slot_path,
                )
            }
        ),
    )


if __name__ == "__main__":
    unittest.main()
