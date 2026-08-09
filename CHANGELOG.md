# Changelog

All notable changes are recorded here. This project follows Keep a Changelog.

## [Unreleased]

### Added

- Added `agent-control start --brief-file <path>` to install a brief at the conventional `<coordination_root>/tasks/<task-id>/brief.md` path before launch, matching `plan add-task`/`plan edit-task`. A brief already present at the conventional path with different content blocks the launch and names both paths instead of being silently overwritten; pass `--overwrite-brief` to replace it deliberately. Identical content is a no-op. The error raised when no brief is present now names the conventional path and mentions `--brief-file`.
- Added CLI aliases so the CLI accepts the same names MCP tools use for the same operation: `plan snapshot` for `plan summary` (MCP `agent_plan_snapshot`) and `inbox get` for `inbox show` (MCP `agent_review_inbox_get`). The existing name remains primary and is what `--help` shows. Added `tests/architecture/test_architecture.py::test_cli_mcp_command_parity`, a pinned MCP-tool-to-CLI-command-path mapping that fails if a new MCP tool ships without a CLI mapping entry, if a mapped CLI path stops parsing, or if a mapping entry goes stale.
- Added live candidate revalidation at use time across model catalog and model routing policies.
- Added model catalog union inspection payload containing `inventory_state` (`listed`, `hidden`, `last_seen`, `absent_from_inventory`), `metadata_state` (`configured`, `rule`, `unconfigured`), and `launch_disposition` (`allow`, `require_override`, `reject`).
- Added catalog `provenance` metadata block reporting `version`, `fetched_at`, `etag`, `client_version`, `snapshot_state` (`current` vs `drifted`), and `on_disk_version`.
- Added durable catalog observations in SQLite (`ModelObservationStore`) supporting historical retention and sticky inventory tracking.
- Added catalog alert detection and reporting for `unclassified_model`, `outranks_configured_ladder`, `metadata_without_inventory`, `inventory_shrank`, `client_version_regressed`, and `snapshot_drifted`.
- Added `agent-control model-catalog --check` CLI flag, returning exit code 1 when any warning-severity alert is present.
- Added pattern rules (`[[control.model_catalog.model_rules]]` and `[[control.claude_model_catalog.model_rules]]`) matching unclassified models by glob, setting `metadata_state = "rule"`, and allowing `premium = true`, quota domain, and capacity units for model families without fabricating rate cards.
- Added `unknown_model_policy` configuration under `[control.model_catalog]` and `[control.claude_model_catalog]` (`allow`, `warn` default, `require_override` fail-safe). Under `require_override`, explicit launches of unclassified models require a nonblank `codex_premium_override_reason`.
- `plan retry`/`agent_plan_retry_task` now accept the same execution overrides as `plan edit-task`/`agent_plan_edit_task` (`--route`, `--slot`, `--backend`, `--workspace-access`, `--read-only`, `--codex-quality-tier`, `--codex-model`, `--codex-reasoning-effort`, `--claude-model`, `--claude-reasoning-effort`, `--codex-premium-override-reason`, `--expected-result-status`, `--controller-gate-mode`), applied to the new attempt only, so a task that failed because of a bad execution config (wrong model, wrong route, a premium model with no override reason) can be repaired instead of requiring the plan to be cancelled and recreated. The circuit-breaker fingerprint check now runs against the fully overridden spec, and `task_retry_requested` events record `changed_fields` when the retry changes the execution config.
- Added missing-result checkpoint salvage: when a worker exits 0 with a dirty workspace and never writes `result.md` (`runner_failure == "exited_without_result"`), terminal checkpointing now runs the route's controller quality gates against that checkpoint before cleanup, on native/`controller`-policy routes, so the durable review-inbox record carries gate verdicts instead of only a diff. The job still ends failed and `review_ready` still requires a `Status: completed` result, so gate evidence alone can never reach root acceptance. Every other dirty-after-failure cause (timeout, guardrail violation, tool-call budget, ...) is unaffected. See `docs/recovery-matrix.md`.
- `agent_watch_job` and `agent_start_job(..., wait=True)` now report `settled` and `on_contract` in their compact watch payload, reusing `is_settled`/`is_on_contract` from `features/job_watch` so the MCP surface can see the same on-contract verdict the CLI's `watch --events` exit code is built on. `on_contract` stays `null` until `settled` is `true` — "not yet decided" is never conflated with "decided against". See `docs/operations.md#mcp-coordinators-watch-parity-and-its-limits` for what this does and does not close: the 300 s `_MAX_LONG_POLL_SEC` cap is unchanged (by design), and a non-blocking multi-job cursor poll was evaluated and deliberately not built — see that section for why.
- Added `route_root_ignore_globs` per route (`[routes.<name>] route_root_ignore_globs = [...]`), a list of forward-slash globs the route-root guard excludes from violation determination so operator-owned paths inside the route root (e.g. a kanban board) can change while a slot job runs without tripping `guardrail_violation`. A pattern that would match the whole tree (a bare `**`, or an equivalent) is rejected at config load. The ignore list only narrows what counts as a violation; dirty-state preservation and the preserved status/patch artifacts still cover every changed path, and a violation message names any ignored paths that also changed. No ignore list configured reproduces prior behaviour exactly.
- Claude workers now deny `Bash` calls carrying `run_in_background`, closing the exact move that produced three `runner_failure=exited_without_result` jobs on 2026-08-09: `Monitor` is deliberately excluded from `claude_allowed_tools`, but `run_in_background` lives inside the `Bash` tool itself, so a headless worker could still start background work it can never be notified about and then hang waiting for a notification that never arrives. `ClaudeExecRunner._build_command` now always passes an inline `--settings` JSON `PreToolUse` hook (`scripts/claude_deny_background_bash.py`) that denies any `Bash` call with `run_in_background` truthy. Passed inline rather than committed to `.claude/settings.json`, so it binds spawned worker processes only, never a human's interactive session in the same worktree; applies unconditionally including under `yolo`, since `--dangerously-skip-permissions` bypasses the permission system, not hooks. See `docs/claude-worker-routing.md`.
)
)

### Fixed

- `agent-control statuses` no longer rejects `--config`. It still requires no config and never loads or validates the path it is handed; scripts that pass `--config` uniformly to every invocation no longer need to special-case `statuses`.

### Changed

- Routes or explicit launches pinned to `codex_model = "default"` (or Claude `claude_model = "default"`) resolve to the priority 1 candidate in current inventory; if priority 1 is a premium model, explicit-profile launch requires an override reason and will fail closed without it.
- **Operator Note**: Existing running ACP MCP servers must be restarted before they benefit from model catalog updates.

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
