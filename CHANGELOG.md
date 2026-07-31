# Changelog

All notable changes are recorded here. This project follows Keep a Changelog.

## [Unreleased]

## [0.2.0] - 2026-07-31

### Added

- Added `agent-control mcp wire [--config PATH] [--apply] [--print]` command to discover client repositories for a workspace configuration (from `[routes.*] path` and `[control] coordination_root`) and generate or merge a project-scoped `.mcp.json` containing the derived HTTP server URL into each client repository. Documented the one-instance-per-config MCP model, working-directory discovery, deterministic ports, and lazy server management via `mcp ensure`.
- Added optional `streamable-http` transport support to the MCP server (`--transport streamable-http`, `--host`, `--port`), defaulting to `stdio` and binding `127.0.0.1:8766` by default. HTTP transport runs FastMCP in `stateless_http` mode (`stateless_http=True`) to prevent per-session transport accumulation.
- Added a server-side ceiling on long-polling wait budgets across `agent_watch_job`, `agent_plan_watch`, `agent_plan_run_until_review`, and `agent_start_job(wait=True)`, clamping timeouts above 300.0s down to 300.0s (`timeout_clamped_to: 300.0`), enforcing minimum poll intervals of 0.5s for non-zero timeouts, and resolving `agent_plan_run_until_review(timeout_sec=None)` to 300.0s.

- Routes can declare their own `slot_root`. Dynamic `slots create`, `slots bootstrap`, and
  the slot-path guardrail now resolve the slot directory per route instead of always using
  the global `[control] slot_root`, so an unrelated repository never materializes slots
  inside another project's slot directory. Bootstrapping a brand-new route no longer
  inherits the global roots either: it defaults to a sibling `<repo>-agent-slots` directory
  and writes both `worktree_root` and `slot_root` into the generated route table.

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

- Starting an MCP server no longer opens a terminal window on Windows. The server was
  spawned with `DETACHED_PROCESS`, which leaves it with no console at all, so the venv
  launcher's re-exec of the real interpreter made Windows allocate a fresh one and the
  default terminal turned that into a visible window which stole focus and lived as long
  as the server. It is spawned with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` now, so
  a windowless console is inherited down the chain.
- `mcp ensure` recognizes a healthy server again. The health probe parsed the response as
  plain JSON, but the streamable transport answers `initialize` with a `text/event-stream`
  frame, so every probe failed: each session start launched another server for a config
  that already had one and then waited out its timeout. The probe reads both shapes.
- `mcp ensure` resolves a config's port under the same lock that starts its server.
  Reading the port outside the lock let a second session observe the first session's
  server mid-boot, conclude the port was taken, and give the same config a second port and
  a second server.
- `port_for` never hands a config a port that the assignments file already records for a
  different config, and accepts an occupied port only when it is this config's own server.
  Exhausting 9230-9329 now raises instead of returning the un-scanned base port, which was
  the collision it had just avoided.
- `port_for` refuses to rewrite assignments it could not read. Any read or parse failure
  used to reset the whole mapping to empty, and the next assignment wrote that back, so a
  single transient failure erased every config's remembered port and stranded the
  `.mcp.json` files written by `mcp wire`. A read error raises, an unparsable file is moved
  aside intact and named in the error, and an empty file still cold-starts.
- Running the test suite no longer disturbs the operator's control planes: tests reached
  the real `~/.agent-control-plane/port-assignments.json` and reserved real ports in
  9230-9329, and left MCP servers running after the run.
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
- Added a builtin Claude model catalog (claude-opus-5, claude-sonnet-5, claude-fable-5,
  claude-opus-4-7, claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5) with reasoning
  efforts low/medium/high, plus xhigh/max where supported, and a `default` selector that
  resolves to claude-opus-5. claude-opus-4-8 is no longer a catalog entry; an explicit
  launch of it still passes through unvalidated and ungated, like any unknown model.
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
