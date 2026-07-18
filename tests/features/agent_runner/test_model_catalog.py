from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from agent_control_plane.app.runtime.orchestrator import AgentControlPlane
from agent_control_plane.features.agent_runner.lib.model_catalog import (
    CatalogModelMetadata,
    CatalogRate,
    ModelCatalog,
)


class ModelCatalogTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
