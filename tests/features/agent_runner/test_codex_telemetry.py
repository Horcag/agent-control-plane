from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_control_plane.features.agent_runner.lib.codex_telemetry import (
    codex_turn_completed,
    parse_codex_jsonl,
    render_codex_json_line,
)
from agent_control_plane.features.agent_runner.lib.model_catalog import (
    CatalogModelMetadata,
    CatalogRate,
    ModelCatalog,
)


def _catalog_with_rates(root: Path, model: str) -> ModelCatalog:
    cache_path = root / "models_cache.json"
    cache_path.write_text(json.dumps({"models": [{"slug": model}]}), encoding="utf-8")
    return ModelCatalog.load(
        cache_path=cache_path,
        max_cache_age_sec=60.0,
        metadata=(
            CatalogModelMetadata(
                model=model,
                credit_rate=CatalogRate(62.5, 6.25, 375.0),
                api_usd_rate=CatalogRate(2.5, 0.25, 15.0),
                rate_card_version="test-v1",
                rate_card_source="test",
            ),
        ),
    )


class CodexTelemetryTest(unittest.TestCase):
    def test_parses_usage_tools_failures_and_costs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attempt.events.jsonl"
            events = [
                {"type": "thread.started", "thread_id": "thread-1"},
                {
                    "type": "item.started",
                    "item": {
                        "id": "item-1",
                        "type": "mcp_tool_call",
                        "server": "agentbridge_idea_8644",
                        "tool": "read_file",
                        "status": "in_progress",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "mcp_tool_call",
                        "server": "agentbridge_idea_8644",
                        "tool": "read_file",
                        "status": "completed",
                        "error": None,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-2",
                        "type": "command_execution",
                        "command": "pytest",
                        "status": "failed",
                        "error": "exit 1",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {"id": "item-3", "type": "agent_message", "text": "Done"},
                },
                {"type": "error", "message": "transient warning"},
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 600,
                        "output_tokens": 200,
                        "reasoning_output_tokens": 50,
                    },
                },
            ]
            path.write_text(
                "# non-JSON runner header is ignored\n"
                + "\n".join(json.dumps(event) for event in events)
                + "\n",
                encoding="utf-8",
            )

            metrics = parse_codex_jsonl(
                path,
                model="gpt-5.6-terra",
                duration_sec=20.0,
                catalog=_catalog_with_rates(Path(temp), "gpt-5.6-terra"),
            )

            self.assertTrue(codex_turn_completed(path))
            self.assertTrue(metrics.usage_available)
            self.assertEqual(metrics.thread_id, "thread-1")
            self.assertTrue(metrics.turn_completed)
            self.assertEqual(metrics.event_count, 7)
            self.assertEqual(metrics.input_tokens, 1000)
            self.assertEqual(metrics.cached_input_tokens, 600)
            self.assertEqual(metrics.uncached_input_tokens, 400)
            self.assertEqual(metrics.output_tokens, 200)
            self.assertEqual(metrics.reasoning_output_tokens, 50)
            self.assertEqual(metrics.tool_calls, 2)
            self.assertEqual(metrics.failed_tool_calls, 1)
            self.assertEqual(metrics.error_events, 1)
            self.assertEqual(
                metrics.tool_counts,
                (
                    ("command_execution", 1),
                    ("mcp:agentbridge_idea_8644/read_file", 1),
                ),
            )
            self.assertAlmostEqual(metrics.cache_hit_ratio, 0.6)
            self.assertAlmostEqual(metrics.output_tokens_per_sec, 10.0)
            self.assertAlmostEqual(metrics.estimated_credits or 0.0, 0.10375)
            self.assertAlmostEqual(metrics.estimated_api_usd or 0.0, 0.00415)

    def test_recovers_cumulative_usage_from_codex_session_after_failed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event_log = root / "attempt.events.jsonl"
            event_log.write_text(
                json.dumps({"type": "thread.started", "thread_id": "thread-1"})
                + "\n"
                + json.dumps({"type": "turn.failed", "message": "capacity"})
                + "\n",
                encoding="utf-8",
            )
            session_dir = root / "sessions" / "2026" / "07" / "11"
            session_dir.mkdir(parents=True)
            (session_dir / "rollout-2026-thread-1.jsonl").write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 4000,
                                    "cached_input_tokens": 2500,
                                    "output_tokens": 300,
                                    "reasoning_output_tokens": 80,
                                }
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            metrics = parse_codex_jsonl(
                event_log,
                model="gpt-5.6-terra",
                duration_sec=10.0,
                sessions_root=root / "sessions",
                catalog=_catalog_with_rates(root, "gpt-5.6-terra"),
            )

            self.assertTrue(metrics.usage_available)
            self.assertFalse(metrics.turn_completed)
            self.assertEqual(metrics.input_tokens, 4000)
            self.assertEqual(metrics.cached_input_tokens, 2500)
            self.assertEqual(metrics.output_tokens, 300)
            self.assertEqual(metrics.reasoning_output_tokens, 80)
            self.assertIsNotNone(metrics.estimated_credits)

    def test_unknown_model_keeps_raw_usage_without_estimated_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attempt.events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 0,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )

            metrics = parse_codex_jsonl(path, model="custom-model", duration_sec=1.0)

            self.assertTrue(metrics.usage_available)
            self.assertIsNone(metrics.estimated_credits)
            self.assertIsNone(metrics.estimated_api_usd)

    def test_uses_catalog_rate_metadata_for_an_invented_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "attempt.events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 1_000,
                            "cached_input_tokens": 600,
                            "output_tokens": 200,
                            "reasoning_output_tokens": 50,
                        },
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "models_cache.json"
            cache_path.write_text(
                json.dumps({"models": [{"slug": "future-codex"}]}),
                encoding="utf-8",
            )
            catalog = ModelCatalog.load(
                cache_path=cache_path,
                max_cache_age_sec=60.0,
                metadata=(
                    CatalogModelMetadata(
                        model="future-codex",
                        credit_rate=CatalogRate(2.0, 0.2, 12.0),
                        api_usd_rate=CatalogRate(1.0, 0.1, 6.0),
                        rate_card_version="future-v1",
                        rate_card_source="test",
                    ),
                ),
            )

            metrics = parse_codex_jsonl(
                path,
                model="future-codex",
                duration_sec=1.0,
                catalog=catalog,
            )

            self.assertAlmostEqual(metrics.estimated_credits or 0.0, 0.00332)
            self.assertAlmostEqual(metrics.estimated_api_usd or 0.0, 0.00166)
            self.assertEqual(metrics.rate_card_version, "future-v1")

    def test_renders_guardrail_compatible_tool_markers(self) -> None:
        mcp_line = json.dumps(
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "agentbridge_idea_8644",
                    "tool": "read_file",
                },
            }
        )
        exec_line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "git status",
                    "status": "completed",
                },
            }
        )
        web_line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "web_search",
                    "query": "current docs",
                    "status": "completed",
                },
            }
        )

        self.assertEqual(
            render_codex_json_line(mcp_line),
            "mcp: agentbridge_idea_8644/read_file started\n",
        )
        self.assertIn("\nexec\n", "\n" + render_codex_json_line(exec_line))
        self.assertIn("web search:", render_codex_json_line(web_line))


if __name__ == "__main__":
    unittest.main()
