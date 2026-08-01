from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane
from agent_control_plane.features.agent_runner.lib.model_catalog import (
    CatalogModelMetadata,
    CatalogModelRule,
    CatalogRate,
    ModelCatalog,
)


class ModelCatalogTest(unittest.TestCase):
    def test_model_rule_resolution_order_and_metadata_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-5.6-nova", "visibility": "visible"},
                            {"slug": "gpt-5.6-luna", "visibility": "visible"},
                            {"slug": "unmatched-model", "visibility": "visible"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            exact_meta = CatalogModelMetadata(model="gpt-5.6-luna", premium=False)
            rule1 = CatalogModelRule(match="gpt-5.6-*", premium=True, quota_domain="primary")
            rule2 = CatalogModelRule(match="gpt-5.6-nova", premium=False, quota_domain="spark")

            catalog = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                metadata=(exact_meta,),
                model_rules=(rule1, rule2),
            )

            # 1. gpt-5.6-nova matches rule1 (declaration order wins over rule2)
            meta_nova = catalog.rate_metadata_for("gpt-5.6-nova")
            self.assertIsNotNone(meta_nova)
            assert meta_nova is not None
            self.assertTrue(meta_nova.premium)
            self.assertEqual(catalog.launch_disposition("gpt-5.6-nova"), "require_override")

            payload = catalog.inspection_payload()
            models_by_slug = {m["model"]: m for m in payload["models"]}

            self.assertEqual(models_by_slug["gpt-5.6-nova"]["metadata_state"], "rule")
            self.assertEqual(models_by_slug["gpt-5.6-nova"]["premium_state"], "known")
            self.assertTrue(models_by_slug["gpt-5.6-nova"]["premium"])

            # 2. Exact entry gpt-5.6-luna beats matching rule1
            self.assertEqual(models_by_slug["gpt-5.6-luna"]["metadata_state"], "configured")
            self.assertFalse(models_by_slug["gpt-5.6-luna"]["premium"])
            self.assertEqual(catalog.launch_disposition("gpt-5.6-luna"), "allow")

            # 3. Unmatched model stays unclassified
            self.assertEqual(models_by_slug["unmatched-model"]["metadata_state"], "unconfigured")
            self.assertEqual(models_by_slug["unmatched-model"]["premium_state"], "unknown")
            self.assertIsNone(models_by_slug["unmatched-model"]["premium"])

    def test_loads_visible_cache_model_and_merges_explicit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "future-codex",
                                "visibility": "visible",
                                "priority": 7,
                                "default_reasoning_level": "high",
                                "supported_reasoning_levels": [
                                    {"effort": "low", "description": "Fast"},
                                    {"effort": "high", "description": "Deep"},
                                    {"effort": "ultra", "description": "Future"},
                                ],
                                "unknown_future_field": {"kept": "out of policy"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            metadata = CatalogModelMetadata(
                model="future-codex",
                quota_domain="separate",
                capacity_units=(("low", 3), ("high", 9), ("ultra", 18)),
                credit_rate=CatalogRate(2.0, 0.2, 12.0),
                api_usd_rate=CatalogRate(1.0, 0.1, 6.0),
                rate_card_version="future-v1",
                rate_card_source="operator-verified",
            )

            catalog = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                metadata=(metadata,),
            )

            model = catalog.model("future-codex")

            self.assertIsNotNone(model)
            if model is None:
                self.fail("Expected the cache model to be visible")
            self.assertTrue(model.visible)
            self.assertEqual(model.priority, 7)
            self.assertEqual(model.default_reasoning_effort, "high")
            self.assertEqual(model.supported_reasoning_efforts, ("low", "high", "ultra"))
            self.assertEqual(catalog.quota_domain_for("future-codex"), "separate")
            self.assertEqual(
                catalog.capacity_units_for("future-codex", "ultra", full_capacity=30), 18
            )
            self.assertEqual(catalog.rate_metadata_for("future-codex"), metadata)
            self.assertFalse(metadata.premium)

    def test_premium_metadata_is_optional_and_visible_in_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "expensive-future", "supported_reasoning_levels": ["medium"]}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                metadata=(CatalogModelMetadata(model="expensive-future", premium=True),),
            )

            payload = catalog.inspection_payload()

            self.assertTrue(payload["models"][0]["premium"])

    def test_missing_metadata_is_unknown_in_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {"models": [{"slug": "ordinary-future", "supported_reasoning_levels": ["low"]}]}
                ),
                encoding="utf-8",
            )
            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)

            model = catalog.inspection_payload()["models"][0]
            self.assertIsNone(model["premium"])
            self.assertEqual(model["premium_state"], "unknown")

    def test_malformed_reasoning_entries_invalidate_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "future-codex",
                                "supported_reasoning_levels": [
                                    {"description": "Missing the effort"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)

            self.assertEqual(catalog.cache_status, "invalid")
            self.assertIsNone(catalog.model("future-codex"))

    def test_hidden_model_is_not_an_automatic_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "hidden-future-codex",
                                "visibility": "hidden",
                                "supported_reasoning_levels": [{"effort": "low"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)

            with self.assertRaisesRegex(ValueError, "not visible"):
                catalog.validate_automatic_profile("hidden-future-codex", "low")

    def test_missing_invalid_and_stale_cache_never_expose_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"

            missing = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            self.assertEqual(missing.cache_status, "missing")
            self.assertIsNone(missing.model("future-codex"))

            cache_path.write_text("not-json", encoding="utf-8")
            invalid = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            self.assertEqual(invalid.cache_status, "invalid")
            self.assertIsNone(invalid.model("future-codex"))

            cache_path.write_text(json.dumps({"models": []}), encoding="utf-8")
            stale_timestamp = time.time() - 120.0
            os.utime(cache_path, (stale_timestamp, stale_timestamp))
            stale = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            self.assertEqual(stale.cache_status, "stale")
            self.assertIsNone(stale.model("future-codex"))

    def test_inspection_payload_is_bounded_and_explicit_about_missing_prices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "unpriced-codex",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                                "instructions": "do not expose this cache blob",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)

            payload = catalog.inspection_payload()

            self.assertEqual(payload["status"], "loaded")
            self.assertEqual(payload["models"][0]["model"], "unpriced-codex")
            self.assertIsNone(payload["models"][0]["rate_card_version"])
            self.assertIsNone(payload["models"][0]["rate_card_source"])
            self.assertFalse(payload["models"][0]["has_credit_rate"])
            self.assertFalse(payload["models"][0]["has_api_usd_rate"])
            self.assertNotIn("instructions", payload["models"][0])

    def test_control_plane_returns_model_catalog_inspection_payload(self) -> None:
        catalog = ModelCatalog(
            models={},
            metadata={},
            cache_status="missing",
            source="models_cache.json",
            version=None,
        )
        control = AgentControlPlane.__new__(AgentControlPlane)
        control.model_catalog = catalog

        self.assertEqual(control.model_catalog_inspection(), catalog.inspection_payload())

    def test_cache_changes_after_load_are_observed_without_rebuilding_catalog(self) -> None:
        """Test 1: Revalidation on stat change updates the snapshot on the same catalog instance."""
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "model-v1",
                                "visibility": "visible",
                                "supported_reasoning_levels": [{"effort": "low"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)

            self.assertIsNotNone(catalog.model("model-v1"))
            self.assertIsNone(catalog.model("model-v2"))

            time.sleep(0.01)
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "model-v1",
                                "visibility": "visible",
                                "supported_reasoning_levels": [{"effort": "low"}],
                            },
                            {
                                "slug": "model-v2",
                                "visibility": "visible",
                                "supported_reasoning_levels": [{"effort": "high"}],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            new_mtime = time.time() + 2.0
            os.utime(cache_path, (new_mtime, new_mtime))

            self.assertIsNotNone(catalog.model("model-v2"))
            self.assertEqual(catalog.model("model-v2").default_reasoning_effort, None)

    def test_union_of_cache_inventory_and_acp_metadata(self) -> None:
        """Test 2: Payload includes union of cache inventory and metadata with state markers."""
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "cache-only-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": [{"effort": "low"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            metadata_only = CatalogModelMetadata(model="gpt-5.6-terra", premium=True)
            catalog = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                metadata=(metadata_only,),
            )

            payload = catalog.inspection_payload()
            models_by_name = {m["model"]: m for m in payload["models"]}

            self.assertIn("cache-only-model", models_by_name)
            cache_entry = models_by_name["cache-only-model"]
            self.assertEqual(cache_entry["inventory_state"], "listed")
            self.assertEqual(cache_entry["metadata_state"], "unconfigured")
            self.assertEqual(cache_entry["premium_state"], "unknown")
            self.assertIsNone(cache_entry["premium"])

            self.assertIn("gpt-5.6-terra", models_by_name)
            meta_entry = models_by_name["gpt-5.6-terra"]
            self.assertEqual(meta_entry["inventory_state"], "absent_from_inventory")
            self.assertEqual(meta_entry["metadata_state"], "configured")
            self.assertEqual(meta_entry["premium_state"], "known")
            self.assertTrue(meta_entry["premium"])
            self.assertIsNone(meta_entry["visible"])
            self.assertIsNone(meta_entry["priority"])
            self.assertIsNone(meta_entry["default_reasoning_effort"])
            self.assertEqual(meta_entry["supported_reasoning_efforts"], [])

    def test_launch_disposition_and_launcher_refusal_for_metadata_only_premium_model(self) -> None:
        """Test 3: Metadata-only premium model has require_override disposition and is rejected by launcher without override."""
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(json.dumps({"models": []}), encoding="utf-8")

            premium_meta = CatalogModelMetadata(model="gpt-5.6-terra", premium=True)
            catalog = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                metadata=(premium_meta,),
            )

            payload = catalog.inspection_payload()
            meta_entry = next(m for m in payload["models"] if m["model"] == "gpt-5.6-terra")
            self.assertEqual(meta_entry["premium_state"], "known")
            self.assertEqual(meta_entry["launch_disposition"], "require_override")

            self.assertEqual(catalog.launch_disposition("gpt-5.6-terra"), "require_override")
            self.assertEqual(
                catalog.launch_disposition(
                    "gpt-5.6-terra", override_reason="Approved for benchmark"
                ),
                "allow",
            )

            from agent_control_plane.features.agent_runner.lib.job_launcher import JobLaunchError
            from agent_control_plane.features.agent_runner.lib.model_routing import (
                ModelProfile,
                ModelRoutingPolicy,
            )

            routing = ModelRoutingPolicy(
                policies={},
                mechanical=ModelProfile("gpt-5.6-terra", "low"),
                balanced=ModelProfile("gpt-5.6-terra", "low"),
                deep=ModelProfile("gpt-5.6-terra", "low"),
                mechanical_tool_call_budget=10,
                balanced_tool_call_budget=10,
                deep_tool_call_budget=10,
                catalog=catalog,
            )

            class DummyLauncher:
                def __init__(self, routing_policy: ModelRoutingPolicy):
                    self.model_routing = routing_policy

                def check_launch(self, model_name: str, override_reason: str | None = None):
                    snapshot = self.model_routing.catalog.snapshot
                    disposition = snapshot.launch_disposition(
                        model_name,
                        override_reason=override_reason,
                    )
                    if disposition == "require_override":
                        raise JobLaunchError(
                            "Explicit launch of premium Codex model requires a nonblank "
                            "codex_premium_override_reason"
                        )
                    return disposition

            launcher = DummyLauncher(routing)
            with self.assertRaisesRegex(JobLaunchError, "codex_premium_override_reason"):
                launcher.check_launch("gpt-5.6-terra", override_reason=None)

            self.assertEqual(
                launcher.check_launch("gpt-5.6-terra", override_reason="Budget approved"),
                "allow",
            )

    def test_drifted_snapshot_served_when_re_read_fails(self) -> None:
        """Test 4: Drifted path serves last good snapshot with snapshot_state='drifted' on re-read failure."""
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "good-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": [{"effort": "low"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            initial_payload = catalog.inspection_payload()
            self.assertEqual(initial_payload["status"], "loaded")
            self.assertEqual(initial_payload["snapshot_state"], "current")

            time.sleep(0.01)
            cache_path.write_text("corrupted json {", encoding="utf-8")

            payload = catalog.inspection_payload()
            self.assertEqual(payload["status"], "loaded")
            self.assertEqual(payload["snapshot_state"], "drifted")
            self.assertEqual(len(payload["models"]), 1)
            self.assertEqual(payload["models"][0]["model"], "good-model")

    def test_concurrent_access_during_cache_file_updates(self) -> None:
        """Test 5: Concurrency - concurrent reads during cache file rewrites never see half-built snapshot."""
        import concurrent.futures

        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "initial-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": [{"effort": "low"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            stop_event = threading.Event()

            def writer():
                counter = 0
                while not stop_event.is_set():
                    counter += 1
                    model_slug = f"model-{counter}"
                    content = json.dumps(
                        {
                            "models": [
                                {
                                    "slug": model_slug,
                                    "visibility": "visible",
                                    "supported_reasoning_levels": [{"effort": "low"}],
                                }
                            ]
                        }
                    )
                    with contextlib.suppress(OSError):
                        cache_path.write_text(content, encoding="utf-8")
                    time.sleep(0.001)

            def reader():
                for _ in range(50):
                    snap = catalog.snapshot
                    payload = catalog.inspection_payload()
                    self.assertIn(payload["snapshot_state"], {"current", "drifted"})
                    self.assertTrue(isinstance(snap.models, dict))
                    time.sleep(0.001)

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(reader) for _ in range(4)]
                    for f in futures:
                        f.result()
            finally:
                stop_event.set()
                writer_thread.join()

    def test_two_successive_loads_shrinking_model_set_sticky_inventory(self) -> None:
        """Requirement 1 & Test 1: Vanished model appears with inventory_state='last_seen' and last_seen_at."""
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                            {
                                "slug": "gpt-5.6-luna",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["high"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cat1 = ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=1700000000.0
            )
            payload1 = cat1.inspection_payload()
            models1 = {m["model"]: m for m in payload1["models"]}
            self.assertEqual(models1["gpt-5.6-terra"]["inventory_state"], "listed")
            self.assertEqual(models1["gpt-5.6-luna"]["inventory_state"], "listed")

            # Shrink model set in cache file
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cat2 = ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=1700000100.0
            )
            payload2 = cat2.inspection_payload()
            models2 = {m["model"]: m for m in payload2["models"]}
            self.assertEqual(models2["gpt-5.6-terra"]["inventory_state"], "listed")
            self.assertIn("gpt-5.6-luna", models2)
            self.assertEqual(models2["gpt-5.6-luna"]["inventory_state"], "last_seen")
            self.assertIsNotNone(models2["gpt-5.6-luna"]["last_seen_at"])

    def test_model_outside_retention_window_drops_out_of_inventory(self) -> None:
        """Requirement 2 & Test 2: Model outside observation window drops out of inventory."""
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "old-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            t0 = 1700000000.0
            ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=t0
            )

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "new-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            t_8days = t0 + (8 * 86400.0)
            cat = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                db_path=db_path,
                now=t_8days,
                observation_window_days=7.0,
            )
            payload = cat.inspection_payload()
            model_names = [m["model"] for m in payload["models"]]
            self.assertIn("new-model", model_names)
            self.assertNotIn("old-model", model_names)

    def test_default_resolution_ignores_history_only_entries(self) -> None:
        """Requirement 3 & Test 3: Automatic 'default' resolution ignores history-only entries."""
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "priority1-old",
                                "priority": 1,
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=1700000000.0
            )

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "priority10-current",
                                "priority": 10,
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            cat = ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=1700000100.0
            )

            resolved = cat.resolve_automatic_profile("default", "low")
            self.assertEqual(resolved, "priority10-current")

    def test_repeat_observation_updates_last_seen_and_does_not_add_row(self) -> None:
        """Requirement 4 & Test 4: Repeat observation updates last_seen_at without duplicate rows."""
        from agent_control_plane.shared.sqlite_runtime import control_database

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "stable-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            t0 = 1700000000.0
            ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=t0
            )

            t1 = 1700000100.0
            ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=t1
            )

            with control_database(db_path) as db:
                rows = db.execute(
                    "select version, first_seen_at, last_seen_at from model_observations"
                ).fetchall()
                self.assertEqual(len(rows), 1)

    def test_observations_pruned_by_retention_and_capped_by_max_rows(self) -> None:
        """Requirement 5 & Test 5: Observations pruned by retention and row count capped."""
        from agent_control_plane.shared.sqlite_runtime import control_database

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"

            t0 = 1700000000.0
            for i in range(5):
                cache_path.write_text(
                    json.dumps(
                        {
                            "models": [
                                {
                                    "slug": f"model-v{i}",
                                    "visibility": "visible",
                                    "supported_reasoning_levels": ["low"],
                                },
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                ModelCatalog.load(
                    cache_path=cache_path,
                    max_cache_age_sec=60.0,
                    db_path=db_path,
                    now=t0 + (i * 10.0),
                    max_observation_rows=3,
                )

            with control_database(db_path) as db:
                rows = db.execute("select count(*) as cnt from model_observations").fetchone()
                self.assertEqual(rows["cnt"], 3)

    def test_on_disk_version_differs_on_drifted_path_and_equals_otherwise(self) -> None:
        """Requirement 6 & Test 6: on_disk_version equals version normally and differs when drifted."""
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "valid-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            p1 = catalog.inspection_payload()
            self.assertEqual(p1["snapshot_state"], "current")
            self.assertEqual(p1["version"], p1["on_disk_version"])

            time.sleep(0.01)
            cache_path.write_text("invalid json content {{{", encoding="utf-8")
            p2 = catalog.inspection_payload()
            self.assertEqual(p2["snapshot_state"], "drifted")
            self.assertNotEqual(p2["version"], p2["on_disk_version"])

    def test_inventory_shrank_alert(self) -> None:
        """Test 1: A shrinking model set produces inventory_shrank naming vanished slugs and client versions."""
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"

            cache_path.write_text(
                json.dumps(
                    {
                        "client_version": "0.144.1",
                        "fetched_at": "2026-08-01T04:31:00Z",
                        "models": [
                            {
                                "slug": f"model-{i}",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            }
                            for i in range(8)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            t0 = 1785558660.0  # 2026-08-01T04:31:00Z
            ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=t0
            )

            cache_path.write_text(
                json.dumps(
                    {
                        "client_version": "0.142.5",
                        "fetched_at": "2026-08-01T04:39:00Z",
                        "models": [
                            {
                                "slug": f"model-{i}",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            }
                            for i in range(6)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cat = ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=t0 + 480.0
            )
            payload = cat.inspection_payload()
            alerts = payload["alerts"]
            shrank_alert = next((a for a in alerts if a["code"] == "inventory_shrank"), None)
            self.assertIsNotNone(shrank_alert)
            self.assertEqual(shrank_alert["severity"], "warning")
            self.assertEqual(shrank_alert["vanished_slugs"], ["model-6", "model-7"])
            self.assertEqual(shrank_alert["current_client_version"], "0.142.5")
            self.assertEqual(shrank_alert["observed_client_version"], "0.144.1")

    def test_unclassified_model_alert(self) -> None:
        """Test 2: A first-seen model with no ACP metadata produces unclassified_model with first_seen_at."""
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"
            t0 = 1785578400.0

            # 1. Baseline observation with configured model
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-08-01T09:00:00Z",
                        "models": [
                            {
                                "slug": "configured-model",
                                "visibility": "visible",
                                "priority": 5,
                                "supported_reasoning_levels": ["low"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(cache_path, (t0, t0))
            ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                db_path=db_path,
                now=t0,
                metadata=(CatalogModelMetadata(model="configured-model"),),
            )

            # 2. Subsequent observation introducing brand-new-model
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-08-01T10:00:00Z",
                        "models": [
                            {
                                "slug": "brand-new-model",
                                "visibility": "visible",
                                "priority": 1,
                                "supported_reasoning_levels": ["low"],
                            },
                            {
                                "slug": "configured-model",
                                "visibility": "visible",
                                "priority": 5,
                                "supported_reasoning_levels": ["low"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(cache_path, (t0 + 10.0, t0 + 10.0))
            cat = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                db_path=db_path,
                now=t0 + 10.0,
                metadata=(CatalogModelMetadata(model="configured-model"),),
            )
            payload = cat.inspection_payload()
            alerts = payload["alerts"]
            unclass = next(
                (
                    a
                    for a in alerts
                    if a["code"] == "unclassified_model" and a["model"] == "brand-new-model"
                ),
                None,
            )
            self.assertIsNotNone(unclass)
            self.assertEqual(unclass["priority"], 1)
            self.assertEqual(unclass["first_seen_at"], "2026-08-01T10:00:10+00:00")
            self.assertEqual(unclass["severity"], "warning")

    def test_outranks_configured_ladder_alert(self) -> None:
        """Test 3: A visible model with better priority than configured policy's first candidate produces outranks_configured_ladder."""
        with tempfile.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "better-unconfigured",
                                "visibility": "visible",
                                "priority": 1,
                                "supported_reasoning_levels": ["low"],
                            },
                            {
                                "slug": "configured-candidate",
                                "visibility": "visible",
                                "priority": 10,
                                "supported_reasoning_levels": ["low"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cat = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            payload = cat.inspection_payload(
                policy_first_candidates={"default": "configured-candidate"}
            )
            alerts = payload["alerts"]
            outranks = next((a for a in alerts if a["code"] == "outranks_configured_ladder"), None)
            self.assertIsNotNone(outranks)
            self.assertEqual(outranks["severity"], "warning")
            self.assertIn("default", outranks["policy_names"])
            self.assertEqual(outranks["visible_model"], "better-unconfigured")
            self.assertEqual(outranks["visible_priority"], 1)
            self.assertEqual(outranks["configured_model"], "configured-candidate")
            self.assertEqual(outranks["configured_priority"], 10)

    def test_client_version_regressed_alert(self) -> None:
        """Test 4: client_version going backwards produces client_version_regressed."""
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            db_path = temp_path / "job_store.db"

            cache_path.write_text(
                json.dumps(
                    {
                        "client_version": "0.144.1",
                        "models": [{"slug": "m1", "supported_reasoning_levels": ["low"]}],
                    }
                ),
                encoding="utf-8",
            )
            ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=1700000000.0
            )

            cache_path.write_text(
                json.dumps(
                    {
                        "client_version": "0.142.5",
                        "models": [{"slug": "m1", "supported_reasoning_levels": ["low"]}],
                    }
                ),
                encoding="utf-8",
            )
            cat = ModelCatalog.load(
                cache_path=cache_path, max_cache_age_sec=60.0, db_path=db_path, now=1700000100.0
            )
            payload = cat.inspection_payload()
            alerts = payload["alerts"]
            regress = next((a for a in alerts if a["code"] == "client_version_regressed"), None)
            self.assertIsNotNone(regress)
            self.assertEqual(regress["severity"], "info")
            self.assertEqual(regress["current_client_version"], "0.142.5")
            self.assertEqual(regress["previous_client_version"], "0.144.1")

    def test_smoke_stays_passed_when_only_alerts_present(self) -> None:
        """Test 5: agent_smoke stays passed when only alerts are present, and fails on pre-existing invariant failures."""
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "unclassified-model",
                                "visibility": "visible",
                                "supported_reasoning_levels": ["low"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = ModelCatalog.load(cache_path=cache_path, max_cache_age_sec=60.0)
            control = AgentControlPlane.__new__(AgentControlPlane)
            control.model_catalog = catalog
            mock_routing = type(
                "MockRouting",
                (),
                {
                    "policy_names": ["default"],
                    "ladder_for_policy": lambda self, name: [],
                },
            )()
            control.model_routing = mock_routing
            mock_config = type(
                "MockConfig",
                (),
                {
                    "defaults": type(
                        "MockDefaults",
                        (),
                        {
                            "codex_quality_tier": "default",
                            "codex_model": "default",
                            "codex_reasoning_effort": "low",
                        },
                    )()
                },
            )()
            control.config = mock_config

            diag = control._smoke_routing_diagnostics({"profile_resolution_errors": {}})
            self.assertEqual(len(diag["failures"]), 0)
            self.assertGreater(len(diag["alerts"]), 0)

    def test_cli_model_catalog_check_exit_code(self) -> None:
        """Test 6: model-catalog --check exits non-zero on a warning alert and zero when only info alerts exist."""
        from agent_control_plane.app.runtime.cli import main

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            config_path = temp_path / "control.toml"
            ws_path = temp_path / "repo"
            ws_path.mkdir(parents=True, exist_ok=True)

            toml_text_info = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{temp_path.as_posix()}/runs/jobs.sqlite3"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{ws_path.as_posix()}"
slot_root = "{temp_path.as_posix()}/slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"

[control.model_catalog]
cache_path = "{cache_path.as_posix()}"
max_cache_age_sec = 60.0

[[control.model_catalog.models]]
model = "present-model"

[[control.model_catalog.models]]
model = "configured-absent-model"

[routes.primary]
path = "{ws_path.as_posix()}"
required_branch = "main"
"""

            # 1. Info alert only: metadata_without_inventory
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "present-model",
                                "visibility": "visible",
                                "priority": 10,
                                "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path.write_text(toml_text_info, encoding="utf-8")
            ret_info = main(["model-catalog", "--config", str(config_path), "--check"])
            self.assertEqual(ret_info, 0)

            # 2. Warning alert: unclassified visible model outranking configured
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "unclass-model",
                                "visibility": "visible",
                                "priority": 1,
                                "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
                            },
                            {
                                "slug": "present-model",
                                "visibility": "visible",
                                "priority": 10,
                                "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ret_warn = main(["model-catalog", "--config", str(config_path), "--check"])
            self.assertEqual(ret_warn, 1)

    def test_launch_on_unclassified_model_carries_alert(self) -> None:
        """Test 7: A launch on an unclassified model carries the alert in the response and records it on the job row."""
        from agent_control_plane.app.runtime.orchestrator import AgentControlPlane, StartOptions
        from agent_control_plane.shared.config import load_config

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            config_path = temp_path / "control.toml"
            ws_path = temp_path / "ws"
            ws_path.mkdir(parents=True, exist_ok=True)

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "unclassified-model",
                                "visibility": "visible",
                                "priority": 1,
                                "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            # Git init ws_path to capture head commit
            import subprocess

            subprocess.run(["git", "init"], cwd=ws_path, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=ws_path, check=True)
            subprocess.run(["git", "config", "user.email", "test@test"], cwd=ws_path, check=True)
            (ws_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=ws_path, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=ws_path, check=True)

            res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=ws_path,
                capture_output=True,
                text=True,
            )
            head_branch = res.stdout.strip()

            toml_text = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{temp_path.as_posix()}/runs/jobs.sqlite3"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{ws_path.as_posix()}"
slot_root = "{temp_path.as_posix()}/slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"

[control.model_catalog]
cache_path = "{cache_path.as_posix()}"
max_cache_age_sec = 60.0

[routes.primary]
path = "{ws_path.as_posix()}"
required_branch = "{head_branch}"
"""
            config_path.write_text(toml_text, encoding="utf-8")
            config = load_config(config_path)
            control = AgentControlPlane(config)

            coordination_root = config.coordination_root
            coordination_root.mkdir(parents=True, exist_ok=True)
            (coordination_root / "agent-protocol.md").write_text("# Protocol\n", encoding="utf-8")
            (coordination_root / "workspace-routing.md").write_text("# Routing\n", encoding="utf-8")
            task_dir = coordination_root / "tasks" / "task-1"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "brief.md").write_text("# Brief", encoding="utf-8")

            options = StartOptions(
                task_id="task-1",
                route="primary",
                codex_model="unclassified-model",
                codex_reasoning_effort="low",
                workspace_path=ws_path,
                expected_branch=head_branch,
            )

            job = control.start_job(options)
            self.assertTrue(hasattr(job, "alerts"))
            alerts = job.alerts
            self.assertGreater(len(alerts), 0)
            self.assertEqual(alerts[0]["code"], "unclassified_model")

            events = control.store.recent_events(job.job_id)
            catalog_alert_events = [e for e in events if e[1] == "catalog_alert"]
            self.assertGreater(len(catalog_alert_events), 0)

    def test_steady_state_quiet_and_mirror_unseen_model_warnings(self) -> None:
        """Test steady state produces zero warning alerts (and exit 0), and an unseen better model produces 2 warnings (and exit non-zero)."""
        from agent_control_plane.app.runtime.cli import main

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            config_path = temp_path / "control.toml"
            db_path = temp_path / "runs" / "jobs.sqlite3"
            (temp_path / "runs").mkdir(parents=True, exist_ok=True)
            ws_path = temp_path / "repo"
            ws_path.mkdir(parents=True, exist_ok=True)

            t1_ts = time.time()
            t0_ts = t1_ts - 60.0 * 86400.0
            t1_iso = datetime.fromtimestamp(t1_ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            t0_iso = datetime.fromtimestamp(t0_ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            max_cache_age_sec = 86400.0

            # Populate observation store at t0 (60 days ago) for old models
            from agent_control_plane.entities.job import ModelObservationStore

            obs_store = ModelObservationStore(db_path)
            old_models = [
                {
                    "model": "gpt-5.6-sol",
                    "visible": True,
                    "priority": 1,
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                },
                {
                    "model": "gpt-5.6-terra",
                    "visible": True,
                    "priority": 2,
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                },
                {
                    "model": "gpt-5.6-luna",
                    "visible": True,
                    "priority": 3,
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                },
                {
                    "model": "gpt-5.5",
                    "visible": True,
                    "priority": 7,
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                },
                {
                    "model": "gpt-5.4",
                    "visible": True,
                    "priority": 16,
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                },
                {
                    "model": "gpt-5.4-mini",
                    "visible": True,
                    "priority": 23,
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                },
                {
                    "model": "gpt-5.3-codex-spark",
                    "visible": True,
                    "priority": 26,
                    "supported_reasoning_efforts": ["low", "medium", "high", "xhigh"],
                },
            ]
            obs_store.record_observation(
                version="v-old",
                content_hash="v-old",
                fetched_at=t0_iso,
                client_version="0.100.0",
                observed_models=old_models,
                now=t0_iso,
                retention_days=90.0,
            )

            # Write models_cache.json with old models
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": t1_iso,
                        "client_version": "0.100.0",
                        "models": [
                            {
                                "slug": m["model"],
                                "visibility": "visible",
                                "priority": m["priority"],
                                "supported_reasoning_levels": [
                                    "low",
                                    "medium",
                                    "high",
                                    "xhigh",
                                ],
                            }
                            for m in old_models
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(cache_path, (t1_ts, t1_ts))

            toml_text = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{db_path.as_posix()}"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{ws_path.as_posix()}"
slot_root = "{temp_path.as_posix()}/slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"

[control.model_catalog]
cache_path = "{cache_path.as_posix()}"
max_cache_age_sec = {max_cache_age_sec}
newness_window_days = 7.0
observation_retention_days = 90.0

[[control.model_catalog.models]]
model = "gpt-5.6-sol"
premium = true

[[control.model_catalog.models]]
model = "gpt-5.6-terra"
premium = true

[[control.model_catalog.models]]
model = "gpt-5.6-luna"
premium = true

[[control.model_catalog.models]]
model = "gpt-5.3-codex-spark"
premium = false

[routes.primary]
path = "{ws_path.as_posix()}"
required_branch = "main"

[routes.primary.routing.policies.default]
candidates = [
    {{ model = "gpt-5.3-codex-spark", reasoning_effort = "low" }}
]
"""
            config_path.write_text(toml_text, encoding="utf-8")

            # 1. Steady state load
            cat = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=max_cache_age_sec,
                db_path=db_path,
                now=t1_ts,
                metadata=(
                    CatalogModelMetadata("gpt-5.6-sol", premium=True),
                    CatalogModelMetadata("gpt-5.6-terra", premium=True),
                    CatalogModelMetadata("gpt-5.6-luna", premium=True),
                    CatalogModelMetadata("gpt-5.3-codex-spark", premium=False),
                ),
                observation_retention_days=90.0,
                newness_window_days=7.0,
            )
            payload = cat.inspection_payload(
                policy_first_candidates={"default": "gpt-5.3-codex-spark"}
            )
            warning_alerts = [a for a in payload["alerts"] if a.get("severity") == "warning"]
            self.assertEqual(len(warning_alerts), 0)

            # CLI --check in steady state must exit 0
            ret_quiet = main(["model-catalog", "--config", str(config_path), "--check"])
            self.assertEqual(ret_quiet, 0)

            # 2. Mirror: Add one brand-new unseen model with better priority than ladder (priority 0)
            new_models = [
                *old_models,
                {
                    "model": "gpt-5.7-super",
                    "visible": True,
                    "priority": 0,
                    "supported_reasoning_efforts": ["low"],
                },
            ]
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": t1_iso,
                        "client_version": "0.100.0",
                        "models": [
                            {
                                "slug": m["model"],
                                "visibility": "visible",
                                "priority": m["priority"],
                                "supported_reasoning_levels": [
                                    "low",
                                    "medium",
                                    "high",
                                    "xhigh",
                                ],
                            }
                            for m in new_models
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(cache_path, (t1_ts + 10.0, t1_ts + 10.0))

            cat_mirror = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=max_cache_age_sec,
                db_path=db_path,
                now=t1_ts + 10.0,
                metadata=(
                    CatalogModelMetadata("gpt-5.6-sol", premium=True),
                    CatalogModelMetadata("gpt-5.6-terra", premium=True),
                    CatalogModelMetadata("gpt-5.6-luna", premium=True),
                    CatalogModelMetadata("gpt-5.3-codex-spark", premium=False),
                ),
                observation_retention_days=90.0,
                newness_window_days=7.0,
            )
            payload_mirror = cat_mirror.inspection_payload(
                policy_first_candidates={"default": "gpt-5.3-codex-spark"}
            )
            warning_alerts_mirror = [
                a for a in payload_mirror["alerts"] if a.get("severity") == "warning"
            ]
            self.assertEqual(len(warning_alerts_mirror), 2)
            warning_codes = {a["code"] for a in warning_alerts_mirror}
            self.assertEqual(warning_codes, {"unclassified_model", "outranks_configured_ladder"})

            unclass_a = next(a for a in warning_alerts_mirror if a["code"] == "unclassified_model")
            self.assertEqual(unclass_a["model"], "gpt-5.7-super")

            outranks_a = next(
                a for a in warning_alerts_mirror if a["code"] == "outranks_configured_ladder"
            )
            self.assertEqual(outranks_a["visible_model"], "gpt-5.7-super")

            # CLI --check must exit non-zero (1)
            ret_warn = main(["model-catalog", "--config", str(config_path), "--check"])
            self.assertEqual(ret_warn, 1)


class TestUnknownModelPolicy(unittest.TestCase):
    def test_invalid_policy_rejected_at_config_load(self) -> None:
        """Test 6: An invalid policy value is rejected at config load."""
        from agent_control_plane.shared.config import load_config

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            config_path = temp_path / "control.toml"

            toml_codex = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{temp_path.as_posix()}/runs/jobs.sqlite3"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{temp_path.as_posix()}/ws"
slot_root = "{temp_path.as_posix()}/slots"

[control.model_catalog]
unknown_model_policy = "invalid"

[routes.primary]
path = "{temp_path.as_posix()}/ws"
"""
            with self.assertRaisesRegex(ValueError, "control.model_catalog.unknown_model_policy"):
                load_config(config_path, config_contents=toml_codex.encode("utf-8"))

            toml_claude = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{temp_path.as_posix()}/runs/jobs.sqlite3"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{temp_path.as_posix()}/ws"
slot_root = "{temp_path.as_posix()}/slots"

[control.claude_model_catalog]
unknown_model_policy = "invalid"

[routes.primary]
path = "{temp_path.as_posix()}/ws"
"""
            with self.assertRaisesRegex(
                ValueError, "control.claude_model_catalog.unknown_model_policy"
            ):
                load_config(config_path, config_contents=toml_claude.encode("utf-8"))

    def test_unknown_model_policy_require_override_allow_warn_codex(self) -> None:
        """Tests 1, 2, 3: require_override, allow, and warn behaviors for Codex unclassified models."""
        from agent_control_plane.app.runtime.orchestrator import (
            AgentControlPlane,
            PolicyError,
            StartOptions,
        )
        from agent_control_plane.features.agent_runner.lib.job_launcher import JobLaunchError
        from agent_control_plane.shared.config import load_config

        for policy in ("require_override", "allow", "warn"):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
                temp_path = Path(temp)
                cache_path = temp_path / "models_cache.json"
                config_path = temp_path / "control.toml"
                ws_path = temp_path / "ws"
                ws_path.mkdir(parents=True, exist_ok=True)

                cache_path.write_text(
                    json.dumps(
                        {
                            "models": [
                                {
                                    "slug": "unclassified-model",
                                    "visibility": "visible",
                                    "priority": 1,
                                    "supported_reasoning_levels": [
                                        "low",
                                        "medium",
                                        "high",
                                        "xhigh",
                                    ],
                                },
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                import subprocess

                subprocess.run(["git", "init"], cwd=ws_path, capture_output=True, check=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=ws_path, check=True)
                subprocess.run(
                    ["git", "config", "user.email", "test@test"], cwd=ws_path, check=True
                )
                (ws_path / "file.txt").write_text("hello", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=ws_path, check=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=ws_path, check=True)

                res = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=ws_path,
                    capture_output=True,
                    text=True,
                )
                head_branch = res.stdout.strip()

                toml_text = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{temp_path.as_posix()}/runs/jobs.sqlite3"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{ws_path.as_posix()}"
slot_root = "{temp_path.as_posix()}/slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"

[control.model_catalog]
cache_path = "{cache_path.as_posix()}"
max_cache_age_sec = 60.0
unknown_model_policy = "{policy}"

[routes.primary]
path = "{ws_path.as_posix()}"
required_branch = "{head_branch}"
"""
                config_path.write_text(toml_text, encoding="utf-8")
                config = load_config(config_path)
                control = AgentControlPlane(config)

                coordination_root = config.coordination_root
                coordination_root.mkdir(parents=True, exist_ok=True)
                (coordination_root / "agent-protocol.md").write_text(
                    "# Protocol\n", encoding="utf-8"
                )
                (coordination_root / "workspace-routing.md").write_text(
                    "# Routing\n", encoding="utf-8"
                )
                task_dir = coordination_root / "tasks" / f"task-{policy}"
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "brief.md").write_text("# Brief", encoding="utf-8")

                if policy == "require_override":
                    # Snapshot launch_disposition check
                    snapshot = control.model_routing.catalog.snapshot
                    self.assertEqual(
                        snapshot.launch_disposition("unclassified-model"), "require_override"
                    )
                    self.assertEqual(
                        snapshot.launch_disposition("unclassified-model", override_reason="Reason"),
                        "allow",
                    )

                    # Launch without reason fails
                    opts_no_reason = StartOptions(
                        task_id=f"task-{policy}",
                        route="primary",
                        codex_model="unclassified-model",
                        codex_reasoning_effort="low",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    with self.assertRaises((JobLaunchError, PolicyError)):
                        control.start_job(opts_no_reason)

                    # Launch with reason succeeds
                    opts_with_reason = StartOptions(
                        task_id=f"task-{policy}",
                        route="primary",
                        codex_model="unclassified-model",
                        codex_reasoning_effort="low",
                        codex_premium_override_reason="Approved",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    job = control.start_job(opts_with_reason)
                    self.assertIsNotNone(job)

                elif policy == "allow":
                    opts = StartOptions(
                        task_id=f"task-{policy}",
                        route="primary",
                        codex_model="unclassified-model",
                        codex_reasoning_effort="low",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    job = control.start_job(opts)
                    # Proceed silently: no unclassified_model alert in job.alerts
                    alerts = getattr(job, "alerts", [])
                    unclass_alerts = [a for a in alerts if a.get("code") == "unclassified_model"]
                    self.assertEqual(len(unclass_alerts), 0)

                elif policy == "warn":
                    opts = StartOptions(
                        task_id=f"task-{policy}",
                        route="primary",
                        codex_model="unclassified-model",
                        codex_reasoning_effort="low",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    job = control.start_job(opts)
                    alerts = getattr(job, "alerts", [])
                    unclass_alerts = [a for a in alerts if a.get("code") == "unclassified_model"]
                    self.assertEqual(len(unclass_alerts), 1)

    def test_route_pinned_to_default_resolving_to_premium(self) -> None:
        """Test 4: A route pinned to codex_model = 'default' that resolves to a premium model is refused without override reason and accepted with one."""
        from agent_control_plane.app.runtime.orchestrator import (
            AgentControlPlane,
            PolicyError,
            StartOptions,
        )
        from agent_control_plane.features.agent_runner.lib.job_launcher import JobLaunchError
        from agent_control_plane.shared.config import load_config

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            temp_path = Path(temp)
            cache_path = temp_path / "models_cache.json"
            config_path = temp_path / "control.toml"
            ws_path = temp_path / "ws"
            ws_path.mkdir(parents=True, exist_ok=True)

            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "visibility": "visible",
                                "priority": 1,
                                "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            import subprocess

            subprocess.run(["git", "init"], cwd=ws_path, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=ws_path, check=True)
            subprocess.run(["git", "config", "user.email", "test@test"], cwd=ws_path, check=True)
            (ws_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=ws_path, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=ws_path, check=True)

            res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=ws_path,
                capture_output=True,
                text=True,
            )
            head_branch = res.stdout.strip()

            toml_text = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{temp_path.as_posix()}/runs/jobs.sqlite3"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{ws_path.as_posix()}"
slot_root = "{temp_path.as_posix()}/slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"

[control.model_catalog]
cache_path = "{cache_path.as_posix()}"
max_cache_age_sec = 60.0

[[control.model_catalog.models]]
model = "gpt-5.6-terra"
premium = true

[routes.primary]
path = "{ws_path.as_posix()}"
required_branch = "{head_branch}"
codex_model = "default"
"""
            config_path.write_text(toml_text, encoding="utf-8")
            config = load_config(config_path)
            control = AgentControlPlane(config)

            coordination_root = config.coordination_root
            coordination_root.mkdir(parents=True, exist_ok=True)
            (coordination_root / "agent-protocol.md").write_text("# Protocol\n", encoding="utf-8")
            (coordination_root / "workspace-routing.md").write_text("# Routing\n", encoding="utf-8")
            task_dir = coordination_root / "tasks" / "task-default"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "brief.md").write_text("# Brief", encoding="utf-8")

            # Without override reason -> refused
            opts_refused = StartOptions(
                task_id="task-default",
                route="primary",
                codex_reasoning_effort="low",
                workspace_path=ws_path,
                expected_branch=head_branch,
            )
            with self.assertRaises((JobLaunchError, PolicyError)):
                control.start_job(opts_refused)

            # With override reason -> accepted
            opts_accepted = StartOptions(
                task_id="task-default",
                route="primary",
                codex_reasoning_effort="low",
                codex_premium_override_reason="Approved premium default",
                workspace_path=ws_path,
                expected_branch=head_branch,
            )
            job = control.start_job(opts_accepted)
            self.assertIsNotNone(job)

    def test_claude_unknown_model_policy_mirror(self) -> None:
        """Test 5: The Claude mirror of unknown_model_policy setting behaves the same."""
        from agent_control_plane.app.runtime.orchestrator import (
            AgentControlPlane,
            PolicyError,
            StartOptions,
        )
        from agent_control_plane.features.agent_runner.lib.job_launcher import JobLaunchError
        from agent_control_plane.shared.config import load_config

        for policy in ("require_override", "allow", "warn"):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
                temp_path = Path(temp)
                cache_path = temp_path / "models_cache.json"
                config_path = temp_path / "control.toml"
                ws_path = temp_path / "ws"
                ws_path.mkdir(parents=True, exist_ok=True)

                cache_path.write_text(
                    json.dumps(
                        {
                            "models": [
                                {
                                    "slug": "gpt-5.6-luna",
                                    "visibility": "visible",
                                    "priority": 1,
                                    "supported_reasoning_levels": ["low", "medium", "high"],
                                },
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                import subprocess

                subprocess.run(["git", "init"], cwd=ws_path, capture_output=True, check=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=ws_path, check=True)
                subprocess.run(
                    ["git", "config", "user.email", "test@test"], cwd=ws_path, check=True
                )
                (ws_path / "file.txt").write_text("hello", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=ws_path, check=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=ws_path, check=True)

                res = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=ws_path,
                    capture_output=True,
                    text=True,
                )
                head_branch = res.stdout.strip()

                toml_text = f"""
[control]
coordination_root = "{temp_path.as_posix()}/.agent-work"
runs_root = "{temp_path.as_posix()}/runs"
database = "{temp_path.as_posix()}/runs/jobs.sqlite3"
worktree_root = "{temp_path.as_posix()}/worktrees"
worktree_base = "{ws_path.as_posix()}"
slot_root = "{temp_path.as_posix()}/slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
timeout_sec = 10
idle_timeout_sec = 5
print_timeout = "10s"

[control.model_catalog]
cache_path = "{cache_path.as_posix()}"
max_cache_age_sec = 60.0

[control.claude_model_catalog]
unknown_model_policy = "{policy}"

[routes.primary]
path = "{ws_path.as_posix()}"
required_branch = "{head_branch}"
"""
                config_path.write_text(toml_text, encoding="utf-8")
                config = load_config(config_path)
                control = AgentControlPlane(config)

                coordination_root = config.coordination_root
                coordination_root.mkdir(parents=True, exist_ok=True)
                (coordination_root / "agent-protocol.md").write_text(
                    "# Protocol\n", encoding="utf-8"
                )
                (coordination_root / "workspace-routing.md").write_text(
                    "# Routing\n", encoding="utf-8"
                )
                task_dir = coordination_root / "tasks" / f"task-claude-{policy}"
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "brief.md").write_text("# Brief", encoding="utf-8")

                if policy == "require_override":
                    snapshot = control.claude_model_catalog.snapshot
                    self.assertEqual(
                        snapshot.launch_disposition("unclassified-claude-model"),
                        "require_override",
                    )
                    self.assertEqual(
                        snapshot.launch_disposition(
                            "unclassified-claude-model", override_reason="Reason"
                        ),
                        "allow",
                    )

                    opts_no_reason = StartOptions(
                        task_id=f"task-claude-{policy}",
                        route="primary",
                        backend="claude",
                        claude_model="unclassified-claude-model",
                        claude_reasoning_effort="high",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    with self.assertRaises((JobLaunchError, PolicyError)):
                        control.start_job(opts_no_reason)

                    opts_with_reason = StartOptions(
                        task_id=f"task-claude-{policy}",
                        route="primary",
                        backend="claude",
                        claude_model="unclassified-claude-model",
                        claude_reasoning_effort="high",
                        codex_premium_override_reason="Approved for Claude",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    job = control.start_job(opts_with_reason)
                    self.assertIsNotNone(job)

                elif policy == "allow":
                    opts = StartOptions(
                        task_id=f"task-claude-{policy}",
                        route="primary",
                        backend="claude",
                        claude_model="unclassified-claude-model",
                        claude_reasoning_effort="high",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    job = control.start_job(opts)
                    alerts = getattr(job, "alerts", [])
                    unclass_alerts = [a for a in alerts if a.get("code") == "unclassified_model"]
                    self.assertEqual(len(unclass_alerts), 0)

                elif policy == "warn":
                    opts = StartOptions(
                        task_id=f"task-claude-{policy}",
                        route="primary",
                        backend="claude",
                        claude_model="unclassified-claude-model",
                        claude_reasoning_effort="high",
                        workspace_path=ws_path,
                        expected_branch=head_branch,
                    )
                    job = control.start_job(opts)
                    alerts = getattr(job, "alerts", [])
                    unclass_alerts = [a for a in alerts if a.get("code") == "unclassified_model"]
                    self.assertEqual(len(unclass_alerts), 1)


if __name__ == "__main__":
    unittest.main()
