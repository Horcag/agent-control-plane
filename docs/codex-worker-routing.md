# Codex Worker Routing

## Default

Use `gpt-5.6-terra` with `model_reasoning_effort = "medium"` for delegated coding
workers. Terra is the balanced capability/cost tier, and OpenAI documents `medium` as
the balanced starting point for most agents.

Use overrides intentionally:

- `gpt-5.6-luna` + `low`: strictly mechanical edits, formatting, repetitive test
  updates, or high-volume extraction with an explicit acceptance test.
- `gpt-5.6-luna` + `medium`: bounded, repeatable implementation or audit work with
  exact files and acceptance criteria. This is the Luna default when Luna is selected.
- `gpt-5.6-terra` + `low`: latency-sensitive, already-specified work where the worker
  does not need to resolve architectural ambiguity.
- `gpt-5.6-terra` + `medium`: normal implementation, debugging, and bounded refactors.
- `gpt-5.6-terra` + `high`: complex edge cases or a second-opinion review when Sol is
  unnecessary.
- `gpt-5.6-sol` + `low`: selective high-value second opinion after a Terra pass. It is
  not a cheaper general worker.
- `gpt-5.6-sol` + `medium` or higher: ambiguous architecture, security-sensitive work,
  or recovery after cheaper workers repeatedly fail.
- `gpt-5.6-sol` + `max`: final quality-first owner review when latency and credits are
  secondary.

Do not use Luna `high` or `xhigh` as a routine substitute for Terra. Escalate from Luna
medium to Terra medium when the task needs materially deeper reasoning. Do not use
`ultra` inside this control plane: Ultra delegates to internal subagents, while this
runner already owns external slot fan-out. Combining both makes attribution, cost,
concurrency, and workspace ownership harder to control.

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
acceptance unless the handoff is valid and review-ready.

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

ACP prices concurrent Codex jobs in deterministic capacity units so cheap workers do
not consume the same global allowance as expensive workers:

| Effective profile | Low, minimal, or none | Medium | High, xhigh, or max |
| --- | ---: | ---: | ---: |
| Luna | 2 | 4 | 6 |
| Terra | 5 | 10 | 15 |
| Sol | 10 | 20 | 30 |

`codex_global_max_concurrent_jobs` supplies 30 units per configured concurrency slot.
`codex_global_max_burst_jobs` separately caps the number of worker processes and
defaults to four times that value. For example, a capacity of two slots plus a burst
limit of eight admits up to eight Luna workers when physical route slots are available,
but the weighted budget still admits only two Sol-high workers.

The quota broker stores the effective weight in its SQLite lease. Acquiring or resizing
a lease is one transaction, so simultaneous workers cannot oversubscribe the budget.
A Luna job that escalates to Terra keeps its original lease and waits until the larger
weight fits. Unknown model names are charged the full 30 units to fail closed. Physical
slot ownership remains exclusive and can impose a lower limit than the global quota.

The rate-limit soft cap is independent of these concurrency weights. The legacy config
name `codex_five_hour_soft_limit_percent` is applied to whichever primary window Codex
reports; that window may be longer than five hours. At or above the threshold, ACP
defers all Codex models, including Luna. A future model-aware reserve should be an
explicit policy rather than an accidental bypass of the configured cap.

## Why Medium

Public coding benchmarks compare model tiers, not this repository's exact prompts and
effort settings. They justify Terra as the worker tier, but they do not prove that
`medium` beats `low` for every task. OpenAI's migration guidance explicitly recommends
testing the current effort and one level lower on representative tasks. The control
plane therefore records enough data for a local decision instead of treating a public
benchmark as an effort benchmark.

Compare identical tasks on clean slots at the same commit. Keep the prompt, timeout,
tools, acceptance criteria, and target files fixed. Evaluate:

1. Acceptance checks and reviewer findings.
2. Completion and retry rate.
3. Wall-clock duration.
4. Input, cached input, output, and reasoning tokens.
5. Result status (`completed`, `partial`, or `blocked`) plus reviewer findings.
6. Tool calls, failed tool calls, and error events.
7. Estimated Codex credits and API-equivalent cost.

Do not select `low` only because it is faster. Promote it for a task class only when it
matches `medium` on the acceptance rubric over multiple representative runs.

## Local Canaries (2026-07-10)

Read-only HH vacancy-state audits ran on commit `38639981` with identical acceptance
criteria and clean AgentBridge slots.

### Terra

| Effort | Status | Duration | Input / cached | Output / reasoning | Tools | Credits |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| low | completed | 223.2 s | 828,161 / 567,808 | 4,537 / 1,329 | 28 / 0 failed | 21.52 |
| medium | completed | 221.7 s | 1,046,168 / 945,664 | 5,282 / 1,716 | 25 / 0 failed | 14.17 |
| high | completed | 309.8 s | 900,926 / 725,504 | 8,692 / 4,851 | 21 / 0 failed | 18.76 |
| xhigh | completed | 346.9 s | 1,103,937 / 843,008 | 11,748 / 7,062 | 22 / 0 failed | 25.98 |

Terra medium traced the full pipeline and found the same material risk classes as high.
Low was not faster in this sample, while high and xhigh took substantially longer without
changing the routing decision.

### Luna, Strict Path Protocol

| Effort | Status | Duration | Input / cached | Output / reasoning | Tools | Credits |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| low | partial | 152.1 s | 855,522 / 645,632 | 5,002 / 1,007 | 24 / 1 failed | 7.61 |
| medium | completed | 181.9 s | 746,148 / 556,032 | 6,771 / 1,959 | 22 / 0 failed | 7.16 |
| high | completed | 222.5 s | 877,465 / 742,912 | 9,189 / 4,129 | 26 / 1 failed | 6.60 |
| xhigh | partial | 342.5 s | 1,216,448 / 1,000,704 | 14,455 / 7,947 | 28 / 0 failed | 10.06 |

Luna medium found the core application-state and employer-matching risks and completed
without tool failures. High was 22% slower and added a useful stale-report boundary.
Extra High was 88% slower than medium, did not add another material risk class, and
finished partial after unnecessary verification commands failed. Low was faster but also
partial. Luna medium is therefore the best current Luna setting for bounded work; collect
three to five clean implementation canaries before making Luna the implementation
default.

The first Luna medium and xhigh attempts were excluded: project-wide IDE discovery read
files from another indexed checkout. That failure produced the strict physical-path
protocol and watchdog rules now enforced by the runner. The table's failed-tool value
counts MCP call errors; a command can return a non-zero exit code inside a successful
`run_command` call, which explains partial results with zero failed MCP calls. Each row is
still one canary, not a statistical benchmark. Cached-input ratios dominate the single-run
credit estimate, so credits are not expected to increase monotonically with effort.

### Sol versus Terra, Strict Head-to-Head

These three runs used the same six files, acceptance criteria, physical-path protocol,
commit, and read-only sandbox:

| Model / effort | Status | Duration | Input / cached | Output / reasoning | Tools | Credits |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Terra medium | completed | 152.3 s | 637,842 / 518,656 | 5,103 / 1,605 | 20 / 1 failed | 12.60 |
| Sol low | completed | 179.1 s | 574,675 / 507,904 | 4,560 / 668 | 19 / 0 failed | 18.12 |
| Sol medium | partial | 251.0 s | 776,079 / 711,168 | 7,043 / 1,705 | 24 / 0 failed | 22.29 |

Against the strict Terra medium run, Sol low was 18% slower and used 44% more credits.
It did surface a distinct detail-budget starvation risk, so it remains useful as a
selective second opinion. Sol medium was 65% slower, used 77% more credits, and finished
partial after unnecessary test-runner attempts. This canary supports Terra medium as the
general worker default and rejects lower-effort Sol as a cost-saving default.

The current Codex rate card explains the result: Sol input and cached input cost twice
Terra, while Sol output costs twelve times Terra. A lower Sol effort can reduce reasoning
tokens, but it does not change the model's token rates.

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

Credit estimates use the Codex per-million-token rate card dated 2026-07-09. API cost
estimates use the GPT-5.6 API prices and the documented 90% cached-input discount. Raw
token counts remain authoritative if pricing changes.

## Sources

- [Codex model and effort descriptions](https://learn.chatgpt.com/docs/models)
- [Codex subagent model and reasoning guidance](https://learn.chatgpt.com/docs/agent-configuration/subagents#choosing-models-and-reasoning)
- [GPT-5.6 migration and effort guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Codex token credit rate card](https://learn.chatgpt.com/docs/pricing#what-are-tokens-and-credits)
- [GPT-5.6 launch benchmarks and API pricing](https://openai.com/index/gpt-5-6/)
