# Changelog

All notable changes are recorded here. This project follows Keep a Changelog.

## [Unreleased]

### Added

- The claude backend now supports `workspace_access = "ide_mcp"` (previously native-only),
  reaching the route's IDEA/AgentBridge MCP server the same way Codex does. ACP writes a
  per-job `runs/<job-id>/claude-mcp-config.json` with exactly that one server and passes it
  via `--mcp-config`; combined with `claude_bare`'s `--strict-mcp-config` the worker loads
  that server and nothing else from operator scope. It also appends `mcp__<server>` to the
  worker's `--allowedTools` so the headless worker can call the IDE MCP tools without an
  interactive approval it can never get. The server endpoint resolves from a new
  `[control.claude_mcp_servers.<name>]` override, or by default from the operator's Claude
  config (`~/.claude.json`, relocatable via `[control] claude_config_path`). A claude
  ide_mcp job whose server cannot be resolved fails closed at launch instead of spawning a
  worker with no IDE tools. The prompt's project-root canary is now backend-neutral.

### Fixed

- Read-only claude jobs now work. Headless `claude -p` cannot complete plan mode's
  ExitPlanMode approval, so read-only no longer uses `--permission-mode plan`; instead it
  runs under `default` prompting with the write-capable builtin tools (`Edit`, `Write`)
  dropped from `--allowedTools`, so file mutations are denied. Claude has no
  `--output-last-message`, so the runner now materializes `<attempt>.last-message.md` from
  the worker's final stream-json message, letting the existing read-only result recovery
  produce `result.md`. The ide_mcp prompt gained a read-only variant that inspects through
  IDE MCP read tools and returns its answer for recovery instead of writing files.
- Added `attempt_metrics.cache_creation_input_tokens` (persisted via a pragma-guarded
  `alter table`, backfilled to 0 for existing rows) so the billing-relevant Claude split —
  uncached input vs cache-read vs cache-write — can be reconstructed without re-parsing
  transcripts; `metrics_report` now also reports a derived `uncached_input_tokens` total.
  `load_attempt_metrics`, the `analytics` CLI command, and the `agent_analytics` MCP tool
  gained a `--backend`/`backend` filter so claude and codex fleets can be analyzed
  separately.
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
- Added `[control.defaults] claude_bare` (default `true`), which appends
  `--strict-mcp-config --setting-sources project` to the claude runner command
  so workers never load the operator's user-scope MCP servers, plugins,
  skills, or `CLAUDE.md`, while keeping the CLI's subscription login intact.
- Added `docs/claude-worker-routing.md`, documenting cheap-first model/effort
  selection, premium gating, and worker isolation for the claude backend.

## [0.1.0] - 2026-07-16

### Added

- Documented the public alpha compatibility contract, upgrade procedure, and maintainer release checklist.
- Documented durable job, plan, review-inbox, and verification artifacts.
- Added a self-contained offline demo for the durable pipeline.
- Added process-level recovery drills for interrupted dispatch and finalization, PID identity mismatches, SQLite contention, post-checkpoint edits, and explicit restart/retry behavior.
- Decomposed plan lifecycle ownership into `PlanService` and extracted CLI command modules.
