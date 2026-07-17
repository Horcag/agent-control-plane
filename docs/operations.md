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

With checkpoint policy, finalization records a controller-owned ref and review-inbox
item, verifies both, then cleans the slot back to its prior branch. It never pushes,
merges, moves the branch, or accepts the change. Any late edit or verification failure
keeps the slot dirty and quarantined.

## Plans, dispatch, and review

Create a JSON manifest with executable tasks and dependencies, then run:

```powershell
agent-control plan create --manifest .\plan.json --config .\config\workspaces.toml
agent-control plan dispatch <plan-id> --max-jobs 2 --config .\config\workspaces.toml
agent-control plan run <plan-id> --until-review --max-jobs 2 --config .\config\workspaces.toml
agent-control plan summary <plan-id> --config .\config\workspaces.toml
agent-control plan watch <plan-id> --since <cursor> --timeout-sec 25 --config .\config\workspaces.toml
```

`plan run` cycles dispatch, watch, reconcile, and dispatch, then stops before root
decisions. Claims are one-shot and durable; dispatch or worker failures require
explicit retry:

```powershell
agent-control plan retry <plan-id> <task-id> --config .\config\workspaces.toml
agent-control accept-handoff <plan-id> <task-id> --review-span-id <span-id> --config .\config\workspaces.toml
```

`accept-handoff` atomically validates verification, resolves the inbox item, records
root acceptance, and unlocks dependants. Delivery is not acceptance. Use `inbox list`,
`inbox show`, and `inbox sync-subagents` for bounded handoff inspection. Use `review
start`, `review checkpoint`, `review attach`, and `review finish` to account for root
review separately.

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
