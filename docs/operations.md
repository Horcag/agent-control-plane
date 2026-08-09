# Operations guide

This guide describes the public, local-first workflow. Keep `config/workspaces.toml`
machine-local; use the tracked example as the starting point and never publish local
paths, credentials, session files, or generated runs.

## Configure and smoke-test

```powershell
Copy-Item .\config\workspaces.example.toml .\config\workspaces.toml
agent-control smoke --config .\config\workspaces.toml
```

Set the route path, required branch, slot paths, `source_roots`, `test_roots`, and
runner commands. Relative paths resolve from the repository root. For Codex choose
`workspace_access = "native"` for native shell/search/editing, or `ide_mcp` when the
configured IDE integration is required. `native` is Codex-only. Keep
`allow_dirty = false`, `yolo = false`, and `terminal_slot_policy = "checkpoint"` unless
there is a reviewed reason to change them. Smoke initializes the ACP SQLite database
and reports route, slot, runner, and archive configuration; it does not launch a job.

## Slots and single jobs

```powershell
agent-control slots sync --config .\config\workspaces.toml
agent-control slots list --route app --config .\config\workspaces.toml
agent-control slots checkout app-1 --branch codex/task --start-point origin/main --config .\config\workspaces.toml
agent-control start --config .\config\workspaces.toml --task-id task --route app --slot app-1 --expected-branch codex/task --wait --live
agent-control watch <job-id> --config .\config\workspaces.toml --live --lines 120
agent-control result <job-id> --config .\config\workspaces.toml
```

The job owns its slot while active. `status`, `summary`, `tail`, `watch`, and `result`
are read-only inspection commands; `cancel` requests cooperative cancellation. A
terminal writable job must leave `result.md` with a plain `Status:` line and a valid
schema-v1 `verification.json`. A completed changed job needs successful checks.

`start` requires a brief at `<coordination_root>/tasks/<task-id>/brief.md` before
launch. Pass `--brief-file <path>` to install one from an arbitrary path in the same
call, mirroring `plan add-task`/`plan edit-task`. If a brief already exists at the
conventional path with different content, `start` fails and names both paths instead
of silently overwriting the operator's brief; pass `--overwrite-brief` to replace it
deliberately. Identical content is always a no-op.

With checkpoint policy, finalization records a controller-owned ref and review-inbox
item, verifies both, then cleans the slot back to its prior branch. It never pushes,
merges, moves the branch, or accepts the change. Any late edit or verification failure
keeps the slot dirty and quarantined.

## Watching jobs as an event stream

Do not write a hand-rolled status poll loop against `status`/`summary`. Use `watch
--events` and read the exit code:

```powershell
agent-control watch <job-id>... --events --config .\config\workspaces.toml
agent-control watch --plan <plan-id> --events --config .\config\workspaces.toml
agent-control watch --task-glob 'acp-watch-*' --events --config .\config\workspaces.toml
```

`--events` accepts multiple positional job ids plus `--plan`/`--task-glob` selection, and
streams one line per event to stdout (flushed immediately), instead of the single JSON
payload the plain `watch <job-id>` form still prints:

```
<ISO8601> <KIND> job=<task_id> status=<s> [result=<r>] [finalization=<f>] [error=<truncated>]
```

`KIND` is one of `START`, `TRANSITION`, `TERMINAL`, `STALE`, `RESUMED`, `WATCH-ERROR`,
`SUMMARY` — uppercase and first after the timestamp so a consumer can filter with a
single grep alternation. A `SUMMARY` line is always printed last. `--stale-after-sec`
(default 300) controls how long a running job may go without a heartbeat before it is
reported `STALE`.

The exit code is the point: it tells a caller what happened without parsing output.

| Exit | Meaning |
| ---- | ------- |
| `0`  | Every watched job ended on-contract (its final `status` matched its declared `expected_result_status`, with finalization `completed`). |
| `1`  | At least one job ended terminal but off-contract (includes `failed`, `guardrail_violation`, and `contract_mismatch`). |
| `3`  | `--timeout-sec` expired with at least one job still non-terminal. |
| `4`  | The selection (job ids / `--plan` / `--task-glob`) matched no jobs, or job state could not be read. |
| `2`  | Argparse usage error. |

This applies to the plain `watch <job-id>` form too (its single JSON payload is
unchanged for existing callers, but its exit code is now meaningful in the same way).

Run `agent-control statuses` (or `--json`) to print the full terminal-status vocabulary
and which statuses are capable of being on-contract, instead of guessing at status
strings. `statuses` accepts `--config` (like every other subcommand) but ignores it,
so scripts that pass `--config` uniformly to every invocation do not need to special-case
this command; it never requires a config to be present.

### Why a worker can't watch anything

`watch --events` is a root tool. A worker's allowlist (`claude_allowed_tools` in
`shared/config.py`) is `Read, Edit, Write, Glob, Grep, Bash`; `Monitor` is deliberately
absent, so a worker's `Monitor` call falls through to Bash permission checking and is
denied. That denial is correct: a worker does bounded work inside one slot and cannot
start jobs, so it has nothing legitimate to watch.

Two failures get misdiagnosed as this permissions issue:

- A worker that backgrounds a long check and ends its turn is reported
  `exited_without_result`, because ending the turn ends the session. Run verification
  synchronously instead.
- A long synchronous run with no tracked-file write can trip the no-progress watchdog
  (`no_progress_timeout_sec`). Prefer light, targeted worker self-checks — the controller
  re-runs the full battery after checkpoint — or raise `no_progress_timeout_sec` if the
  run genuinely needs longer. A watcher is not the fix for either failure.

If an operator decides workers should have watchers anyway, that is a one-line addition
of `Monitor` to `claude_allowed_tools` in configuration, not a code change.

## Plans, dispatch, and review

Create a JSON manifest with executable tasks and dependencies, then run:

```powershell
agent-control plan create --manifest .\plan.json --config .\config\workspaces.toml
agent-control plan dispatch <plan-id> --max-jobs 2 --config .\config\workspaces.toml
agent-control plan run <plan-id> --until-review --max-jobs 2 --config .\config\workspaces.toml
agent-control plan summary <plan-id> --config .\config\workspaces.toml   # alias: plan snapshot
agent-control plan watch <plan-id> --since <cursor> --timeout-sec 25 --config .\config\workspaces.toml
```

`plan run` cycles dispatch, watch, reconcile, and dispatch, then stops before root
decisions. Claims are one-shot and durable; dispatch or worker failures require
explicit retry:

```powershell
agent-control plan retry <plan-id> <task-id> --config .\config\workspaces.toml
agent-control accept-handoff <plan-id> <task-id> --review-span-id <span-id> --config .\config\workspaces.toml
```

When an attempt failed *because of its execution config* (wrong model, wrong route,
wrong slot, a premium model with no override reason), `plan retry` also accepts every
override `plan edit-task` exposes, applied to the new attempt only: `--route`,
`--slot`, `--backend`, `--workspace-access`, `--read-only`, `--codex-quality-tier`,
`--codex-model`, `--codex-reasoning-effort`, `--claude-model`,
`--claude-reasoning-effort`, `--codex-premium-override-reason`,
`--expected-result-status`, and `--controller-gate-mode`. The durable record of the
attempt that already ran is untouched; only the new attempt's execution spec changes,
and the `task_retry_requested` event records which fields changed:

```powershell
agent-control plan retry <plan-id> <task-id> --codex-model gpt-5-mini `
  --codex-premium-override-reason "approved after cost review" `
  --config .\config\workspaces.toml
```

The circuit breaker still compares the fingerprint of the fully overridden attempt
against the prior failure, so a config change that does not touch brief, effective
scope, or tool-call budget does not by itself unblock an escalated task; use
`--retry-override-reason` for that.

`accept-handoff` atomically validates verification, resolves the inbox item, records
root acceptance, and unlocks dependants. Delivery is not acceptance. Use `inbox list`,
`inbox show` (alias: `inbox get`), and `inbox sync-subagents` for bounded handoff
inspection. Use `review start`, `review checkpoint`, `review attach`, and `review
finish` to account for root review separately.

The CLI and MCP surfaces name the same operations differently in a few spots for
historical reasons (`plan summary` / MCP `agent_plan_snapshot`, `inbox show` / MCP
`agent_review_inbox_get`). The CLI accepts the MCP name as an alias in these cases so
either name works; run `agent-control plan --help` or `agent-control inbox --help` to
see the primary (documented) name for each subcommand.

### Spark plan example (explicit model/effort)

```json
{
  "plan_id": "spark-review",
  "title": "Spark review pass",
  "tasks": [
    {
      "task_id": "schema",
      "title": "Review schema changes",
      "execution": {
        "route": "acp",
        "brief": "Analyze schema deltas and provide a compatibility review.",
        "backend": "codex",
        "codex_model": "gpt-5.3-codex-spark",
        "codex_reasoning_effort": "high"
      }
    }
  ]
}
```

```powershell
agent-control plan create --manifest .\spark-review.json --config .\config\workspaces.toml
agent-control plan dispatch spark-review --max-jobs 1 --config .\config\workspaces.toml
```

The plan task carries the explicit model and effort in durable storage, so delayed startup
does not fall back to changed global defaults.

## Quality gates and recovery

Worker/controller native gates are configured on the route. Commands must already be
installed; ACP never installs dependencies. Controller evidence is bound to the
contract hash and checkpoint tree. Missing, failed, timed-out, drifted, or uncovered
evidence blocks review readiness. Keep gates read-only and bounded; `run_on = "both"`
is for cheap checks worth repeating. Deleted Python files are not passed to file-based
linters, and `{changed_python_files}` is sorted and workspace-relative.

After coordinator loss, run ordinary reconciliation first:

```powershell
agent-control reconcile --config .\config\workspaces.toml
agent-control reconcile --job-id <job-id> --terminate-verified-runners --config .\config\workspaces.toml
```

The termination option is opt-in and acts only when durable PID, start identity, and
executable still match. Missing identity, PID reuse, unsupported platform, or any
verification error leaves the runner and slot quarantined. Never kill by process name.

For retention, inspect first and apply only after review records are durable:

```powershell
agent-control archive --older-than-days 14 --limit 50 --config .\config\workspaces.toml
agent-control archive --older-than-days 14 --limit 50 --apply --config .\config\workspaces.toml
```

Retention refuses checkpoint-ref deletion when the stored SHA differs. MCP exposes the
same durable plan, handoff, checkpoint, and reconciliation boundaries; use the CLI when
you need a copyable audit trail. SQLite runs in WAL mode with versioned migrations and
preserves orphan events. On contention, retry the operation after the writer commits;
do not delete or replace the database.

Safety baseline: refuse dirty task workspaces, keep target repositories independent of
ACP, preserve `.git` in native workspace-write, and inspect status/diff before review.
