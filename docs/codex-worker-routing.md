# Codex Worker Routing

ACP controls delegated worker profiles only; the parent/coordinating Codex thread is
external and cannot be selected, downgraded, or escalated by ACP. Routing diagnostics
must not claim knowledge of the actual parent model.

## Model catalog

ACP does not embed a current model list, family rule, capacity weight, or token price in
Python. It loads the live Codex inventory from `~/.codex/models_cache.json` by default;
`[control.model_catalog].cache_path` and `max_cache_age_sec` make that source explicit
and configurable. The inventory supplies a model slug, visibility, priority, default
reasoning effort, and supported reasoning efforts.

`[[control.model_catalog.models]]` is an ACP-owned metadata overlay, not a replacement
inventory. It may assign a quota domain, effort-specific capacity units, and optional
credit/API rate cards. Every configured rate is per million tokens and must include a
rate-card version and source. Set `premium = true` for configuration-owned high-cost
models; omitted metadata safely defaults to `false` and inspection exposes the flag. This
default applies to an existing overlay entry that omits `premium`. A newly visible cached
model with no ACP overlay entry is discoverable without an ACP release, but its ACP
premium metadata is unknown and is reported as `premium = null` /
`premium_state = "unknown"` until an operator adds and configures an overlay entry.

Automatic quality-tier routing accepts only visible, current-cache candidates. It rejects
an invalid effort with the catalog's declared choices, including newly declared `max` or
`ultra`. Missing, invalid, or stale cache input therefore blocks automatic routing rather
than selecting a phantom model. Explicit `--codex-model` and reasoning-effort choices are
never silently replaced: known models are validated by the catalog, while unknown explicit
models remain backend-defined and launchable.

When metadata is absent, ACP preserves raw usage telemetry but reports null cost estimates
and applies the conservative full capacity weight. Configure additional quota domains under
`[[control.model_catalog.quota_domains]]`; their names are arbitrary. The old
`codex_spark_*` settings remain a compatibility bridge only and should be migrated to the
catalog schema in `config/workspaces.example.toml`.

## Workspace Access Modes

Codex jobs support two independent workspace transports:

- `ide_mcp` is the backward-compatible default. It uses the route-selected
  AgentBridge/IDEA server, IDE Git operations, diagnostics, refactors, and one scoped
  terminal tab. Every terminal command must explicitly change to the assigned physical
  workspace because an IDE terminal may ignore its requested working directory.
- `native` runs `codex exec` directly in the assigned slot with Codex-native shell,
  `rg`, and file-edit tools. It uses a much smaller prompt, disables the route-selected
  and known AgentBridge servers, skips IDEA module provisioning, and counts native
  command/file events against the same hard tool budget.

Resolve the effective mode in this order: job override, route override, then
`[control.defaults].workspace_access`. `native` is Codex-only; ACP rejects it for AGY.
Use native mode when lower prompt/tool overhead matters more than IDE diagnostics and
refactors. In the safe `workspace-write` sandbox, `.git` remains protected, so the root
reviewer normally creates the commit after accepting the diff. Read-only native jobs
persist their structured final response through Codex last-message recovery.

Native jobs can add a route-scoped quality contract. `worker` requires successful
machine-readable worker checks; `controller` also reruns matching configured commands
after checkpoint creation and binds the evidence to the contract hash and checkpoint
tree. Use `run_on = "worker"`, `"controller"`, or `"both"` to avoid duplicating expensive
authoritative checks in the worker. Controller mode requires a slot plus
`terminal_slot_policy = "checkpoint"`. Commands run without a shell and must use
dependencies already present in the prepared slot. Configure `include_globs` to select
language-specific checks and keep one universal controller gate for documentation,
configuration, and otherwise-unmapped changes. `{changed_python_files}` expands to the
sorted changed Python files that still exist at the checkpoint, while dependency-aware
tests continue to cover deletions. `native_quality_max_parallel` allows `1..4` concurrent
read-only controller gates. Failed gates block `review_ready` without discarding the
checkpoint. A gate that mutates the worktree causes cleanup to fail closed and
quarantines the slot.

## Terminal Handoff and Slot Release

Set `[control.defaults].terminal_slot_policy = "checkpoint"` when slots should become
reusable after workers finish with uncommitted changes. ACP builds a commit with a
temporary Git index, pins it under `refs/agent-control-plane/jobs/<job-hash>`, verifies
the commit tree, and persists the same SHA plus a hashed full result payload in the review
inbox. Inbox lists remain bounded; the full payload is loaded only by `inbox show`. Only
then does it reset the worktree to its existing branch `HEAD` and mark the slot available.

The checkpoint is delivery state, not acceptance: it does not move a branch, push,
merge, or unlock plan dependencies. Root review may inspect or cherry-pick the ref and
then use `accept-handoff`/`agent_accept_handoff` to commit inbox resolution, plan
acceptance, and review attribution atomically. Failed and cancelled jobs use the same
mechanism as salvage checkpoints.

Cleanup fails closed. A changed `HEAD`, a changed worktree after checkpoint creation, a
dirty nested submodule, an unverifiable ref, or a failed inbox write prevents cleanup.
The durable ref remains available when creation succeeded, and the slot remains dirty or
locked for inspection.

Workers write `result.md` for human review and a sibling schema-v1 `verification.json`
for machine validation. The Markdown status remains the terminal signal, so a missing or
malformed bundle cannot strand a slot. ACP records the bundle as `valid`, `missing`, or
`invalid`, compares changed-file claims with the checkpoint tree, and refuses normal
acceptance unless the handoff is valid and review-ready. Completed changes require at
least one passed zero-exit check; matching worker-stage native gates must be reported
exactly after placeholder expansion, and the machine-readable changed-file set must match
the checkpoint. Controller mode separately requires independently executed,
checkpoint-bound controller-stage evidence.

For multi-task work, put execution specs on durable plan tasks. Use the one-shot
`plan dispatch`/`agent_plan_dispatch` operation for externally scheduled passes, or
`plan run --until-review`/`agent_plan_run_until_review` for a foreground
`dispatch -> watch -> reconcile -> dispatch` cycle. Both paths atomically claim ready
tasks and never retry failures automatically. Use `plan retry` or
`agent_plan_retry_task` only after reviewing the previous failure.

Completed built-in Codex subagents do not have a live callback into ACP. Import their
terminal `task_complete` records from the configured `codex_sessions_root` with
`inbox sync-subagents` or `inbox list --sync-subagents`; ingestion is idempotent and only
accepts rollouts whose working directory belongs to a configured route or slot. Pass
`--parent-thread-id` when a resumed root needs only its own delegated results.

## IDE MCP Routing

Name AgentBridge MCP servers by IDE type and stable port, not by the repository
currently opened in that IDE instance:

- `agentbridge_idea_8644`
- `agentbridge_idea_64343`
- `agentbridge_dataspell_8643`

The names stay unchanged when an IDE instance opens another project. A route selects
the endpoint and independently declares the project root expected at execution time:

```toml
ide_mcp_server = "agentbridge_idea_8644"
ide_mcp_project_root = "C:/path/to/project"
```

The runner derives the native namespace from the server ID, so this example maps to
`mcp__agentbridge_idea_8644__*`. Before any repository operation, the worker must
call `get_project_info` and compare its reported project root with
`ide_mcp_project_root`. A mismatch is a routing failure and must end as
`Status: blocked` before repository reads, edits, Git calls, or commands.

Keep the other AgentBridge endpoints in `codex_disabled_mcp_servers` for that
control-plane config. Switching the project served on an existing port does not
require a Codex restart; the root canary verifies that the endpoint now exposes the
route's expected IDE project.

## Effort Names

The Codex UI and CLI use different labels for two settings:

| UI | CLI / config | Intended use |
| --- | --- | --- |
| Light | `low` | Fast, constrained, low-ambiguity work |
| Medium | `medium` | Balanced default |
| High | `high` | Difficult multi-step work and deeper verification |
| Extra High | `xhigh` | Maximum single-agent reasoning below Max |
| Max | `max` | Give one model more time for the hardest tasks |
| Ultra | `ultra` | Maximum reasoning plus proactive internal subagents |

Luna exposes Light through Extra High. Terra also exposes Ultra on eligible accounts.
The absence of Luna Ultra is meaningful: Ultra is an orchestration mode, not merely one
more reasoning step.

## Weighted Global Quota

Each quota domain declares its own concurrent-job ceiling, burst ceiling, and provider
soft-limit threshold. ACP gives each concurrent slot 30 capacity units, then applies the
metadata-defined weight for the selected model and effort. All leases in one domain share
that domain's counters; a configured model in another domain uses its separate pool.

Acquiring or resizing a lease is one SQLite transaction, so simultaneous workers cannot
oversubscribe a domain. An unknown explicit model uses the full 30-unit cost in `primary`;
physical route-slot ownership may still be more restrictive. Rate-limit snapshots follow
the current model recorded in a rollout and are resolved through the same catalog metadata,
so a future non-primary domain needs no special code path.

To add a domain, first define its limits in `control.model_catalog.quota_domains`, then
assign `quota_domain` on the relevant model overlay. The optional `capacity_units` table
is keyed by reasoning effort. Omitting an effort is conservative: it receives the full
capacity weight rather than a heuristic family-derived value.

## Evidence policy for adaptive routing

Named policies are ordered configuration ladders. The first configured candidate is the
initial worker profile; later candidates form the escalation ladder. Adaptive evidence may
change selection only after its fail-closed evidence requirements are met.

Public coding benchmarks and ad-hoc runs compare sample outcomes, not this repository's
exact prompts and effort settings. They do not determine routing decisions here. Only
durable ACP records that satisfy this section's comparability and root-review rules count as routing evidence.

Routing order is operator configuration. ACP does not import candidate order from external benchmarks or ad-hoc experiments.

Adaptive routing is fail-closed for each named policy:

- Until every candidate has the configured `minimum_samples_per_candidate` repeated
  comparable observations, routing retains the configured fallback, which is the first
  candidate in the operator-supplied ladder. The stable fallback reason is
  `insufficient comparative samples for every candidate`.
- Promotion requires repeated comparable observations for every candidate; the observations
  do not need to be successful, but they must be completed, comparable, and root-reviewed.
- A run is comparable only when route, policy, `task_class` (cohort boundary),
  catalog source/version, automatic selection source, and valid terminal result status
  match exactly. Durable root review (`accepted`/`rejected`/`defects` outcome) must be
  present. Any mismatch or missing value excludes the run.
- This heuristic is not a formal confidence interval or statistical significance test.
  Use conservative settings: `minimum_samples_per_candidate >= 3` and narrow
  task classes for high-risk routing decisions.
- `task_class` groups comparable routes; it is a cohort boundary, not proof of identical
  prompt, context, build mode, tooling, or workspace state.
- Only completed results with durable root acceptance count as quality successes. Missing
  review fails closed; reviewed failures and rejections remain negative evidence, and root
  defects invalidate a candidate through the quality guardrails.
- Unknown price remains `null`. With `allow_missing_price = false`, a candidate with
  missing price is ineligible; only a policy that explicitly allows missing price may
  use it.

Compare identical tasks on clean slots at the same commit. Keep the prompt, timeout,
tools, acceptance criteria, and target files fixed. Evaluate:

1. Acceptance checks and reviewer findings.
2. Completion and retry rate.
3. Wall-clock duration.
4. Input, cached input, output, and reasoning tokens.
5. Result status (`completed`, `partial`, or `blocked`) plus reviewer findings.
6. Tool calls, failed tool calls, and error events.
7. Estimated Codex credits and API-equivalent cost.

Do not reorder or promote candidates from a single run. Use the configured ladder until
the policy's repeated, comparable, root-reviewed observations satisfy every fail-closed
rule above; only successful accepted observations contribute quality successes.

## Telemetry

Each Codex attempt writes a raw `attempt-*.events.jsonl` stream next to the human log.
The runner persists attempt metrics in SQLite, including the result status used for the
reported success rate, and exposes them through:

```powershell
agent-control analytics --config .\config\workspaces.toml
agent-control analytics --config .\config\workspaces.toml --model gpt-5.6-terra --reasoning-effort medium --valid-only
```

`--valid-only` means the telemetry record is structurally complete. It does not prove
that a worker read the assigned checkout or produced a semantically comparable result.
For routing decisions, also verify the result's exact physical workspace, branch, HEAD,
result status, and reviewer rubric.

Credit and API estimates use the checked-in operator-supplied example rates (version
`2026-07-09` is provenance, not a claim that the rate card is official or current). Raw
token counts remain authoritative if pricing changes.

## Sources

- [Codex model and effort descriptions](https://learn.chatgpt.com/docs/models)
- [Codex subagent model and reasoning guidance](https://learn.chatgpt.com/docs/agent-configuration/subagents#choosing-models-and-reasoning)
- [GPT-5.6 migration and effort guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Codex token credit rate card](https://learn.chatgpt.com/docs/pricing#what-are-tokens-and-credits)
- [GPT-5.6 launch benchmarks and API pricing](https://openai.com/index/gpt-5-6/)
