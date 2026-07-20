# Changelog

All notable changes are recorded here. This project follows Keep a Changelog.

## [Unreleased]

### Added

- Added a first-class Claude Code backend (`claude`; `claude-code` is a legacy alias)
  alongside `codex` and `agy`, driven by a headless `claude -p --output-format
  stream-json` runner with `--effort`, `--permission-mode`, and `--session-id`/`--resume`
  support.
- Added a builtin Claude model catalog (claude-opus-4-8, claude-sonnet-5, claude-fable-5,
  claude-opus-4-7, claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5) with reasoning
  efforts low/medium/high, plus xhigh/max where supported, and a `default` selector that
  resolves to claude-opus-4-8.
- Added Claude token accounting: ACP `input_tokens` combines Anthropic input, cache-read,
  and cache-creation tokens; `cached_input_tokens` tracks cache-read tokens;
  `reasoning_output_tokens` is always 0; CLI-reported `total_cost_usd` is stored as
  `estimated_api_usd` with `rate_card_version = "claude-code-cli"`, with fallback rate
  cards available via `[[control.claude_model_catalog.models]]`.
- Added Claude config keys: `[control] claude_command`; `[control.defaults]`
  `claude_model`, `claude_reasoning_effort`, `claude_permission_mode`
  (default/acceptEdits/plan/dontAsk), `claude_allowed_tools`, `claude_sessions_root`,
  `claude_max_turns`; `[[control.claude_model_catalog.models]]` metadata (same shape as
  the Codex model catalog) and `[[control.claude_model_catalog.inventory]]` overrides
  (model, visible, priority, default_reasoning_effort, supported_reasoning_efforts);
  route-level `claude_model`/`claude_reasoning_effort`.
- The claude backend requires `workspace_access = "native"` (no ide_mcp/IDEA support)
  and does not draw from the global Codex quota broker. CLI/MCP/plan surfaces accept
  `--claude-model`/`--claude-reasoning-effort` (`claude_model`/`claude_reasoning_effort`
  keys in plan manifests and `agent_start_job`).

## [0.1.0] - 2026-07-16

### Added

- Documented the public alpha compatibility contract, upgrade procedure, and maintainer release checklist.
- Documented durable job, plan, review-inbox, and verification artifacts.
- Added a self-contained offline demo for the durable pipeline.
- Added process-level recovery drills for interrupted dispatch and finalization, PID identity mismatches, SQLite contention, post-checkpoint edits, and explicit restart/retry behavior.
- Decomposed plan lifecycle ownership into `PlanService` and extracted CLI command modules.
