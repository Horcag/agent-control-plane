# Agent Control Plane

Agent Control Plane is a local job orchestrator for delegating bounded coding tasks to
agent CLIs while preserving route, branch, slot, log, and result state.

It currently supports two runner backends:

- `codex`: runs `codex exec` in the selected workspace.
- `agy`: runs Google Antigravity CLI (`agy`) through a PTY-backed runner.

Codex jobs also support two workspace access modes: the backward-compatible
`ide_mcp` mode through AgentBridge/IDEA and a compact `native` mode that runs directly
in the assigned slot without AgentBridge.

The project is intentionally local-first. It does not host a web service, push code, or
manage cloud credentials for you. It coordinates local repositories and writes durable
run artifacts under `runs/`.

## Why Use It

Use this project when you want an assistant or local automation to hand off a clearly
scoped task to another agent process and then monitor it to a terminal result instead of
leaving work in a vague `queued` or `running` state.

The control plane gives you:

- named routes for repositories and required branches;
- reusable slot worktrees for isolated edit/fix tasks;
- background job execution with logs, prompt files, result files, and SQLite state;
- durable dependency plans with one-shot, crash-safe task dispatch;
- compact review-inbox lists backed by full, hashed result payloads;
- dry-run-first slot cleanup and run archiving;
- guardrails for dirty workspaces and read-only review jobs;
- an optional MCP server for Codex integration.

## Requirements

- Python 3.11 or newer.
- Git available on `PATH`.
- Codex CLI if you use `backend = "codex"`.
- Google Antigravity CLI (`agy`) if you use `backend = "agy"`.
- Windows is the best-tested environment for the `agy` PTY runner. The Codex backend is
  plain subprocess execution and is less Windows-specific.

## Install

From a clone of this repository:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[mcp]"
```

For development checks:

```powershell
python -m pip install -e ".[dev,mcp]"
python -m ruff check src tests
python -m mypy src
python -m bandit -q -c pyproject.toml -r src
python -m pytest
```

CI runs the full suite on both Windows and Linux with Python 3.11 and 3.12;
Ruff, mypy, and Bandit run as separate quality gates.

## Configure

Create a local config from the tracked example:

```powershell
Copy-Item .\config\workspaces.example.toml .\config\workspaces.toml
```

Then edit `config/workspaces.toml` for your machine. Git ignores the local file, so
private paths and local defaults do not get committed.

Keep slot and generated worktree folders beside this repository, not inside it. The
example uses `../agent-control-plane-slots` and `../agent-control-plane-worktrees` to avoid
nested VCS state and noisy IDE indexing in the control-plane checkout.

A minimal route looks like this:

```toml
[control]
coordination_root = ".agent-work"
runs_root = "runs"
database = "runs/jobs.sqlite3"
worktree_root = "../agent-control-plane-worktrees"
worktree_base = "../my-project"
slot_root = "../agent-control-plane-slots"
agy_command = "agy"
codex_command = "codex"

[control.defaults]
backend = "codex"
agy_model = "Gemini 3.5 Flash (High)"
codex_model = "gpt-5.6-terra"
codex_reasoning_effort = "medium"
codex_sandbox_mode = "workspace-write"
workspace_access = "ide_mcp"
terminal_slot_policy = "checkpoint"
allow_dirty = false
yolo = false
prepare_slots = true
runs_layout = "date"
codex_global_max_concurrent_jobs = 2
codex_global_max_burst_jobs = 8

[routes.app]
path = "../my-project"
required_branch = "main"
worktree_root = "../agent-control-plane-worktrees"
worktree_base = "../my-project"
source_roots = ["."]
test_roots = ["tests"]
exclude_dirs = [".venv", "build", "dist", "node_modules"]
ide_sdk_name = "Python 3.12 (my-project)"
ide_sdk_type = "Python SDK"

[slots."app-1"]
route = "app"
path = "../agent-control-plane-slots/app-1"
```

Config paths may be absolute or relative. Relative paths are resolved from the repository
root containing the `config/` directory. `ide_sdk_name` must exactly match an SDK already
registered in IDEA.

Routes that expose overlapping package or symbol names, such as several checkouts of the
same repository, must use a configured SDK. The control plane then creates one isolated
IDEA module per slot, without dependencies between slot modules. Shared modules remain
available only for non-overlapping routes. The generated inspection profile limits duplicate
analysis to `SAME_MODULE`, preserving useful duplicate detection inside a slot while ignoring
identical files in sibling checkouts.

Validate the config before starting jobs:

```powershell
agent-control smoke --config .\config\workspaces.toml
```

`smoke` initializes the SQLite database and reports configured routes, slot state,
available runner commands, and run-archive settings. It does not send a prompt to any
model. ACP-owned databases are bootstrapped once into WAL mode and use versioned,
cross-process migrations. Legacy orphan events are preserved in `orphaned_events`
instead of being discarded. The external Antigravity account database is never migrated
by this runtime.

### Weighted Codex Concurrency

`codex_global_max_concurrent_jobs` is a weighted capacity budget measured in
Sol-high-equivalent slots, not a literal process limit. Each nominal concurrency unit
supplies 30 capacity units. Luna, Terra, and Sol jobs consume fewer or more units
according to the effective model and reasoning effort; unknown model names consume all
30 units.

`codex_global_max_burst_jobs` is the separate hard process-count safety limit and
defaults to four times the weighted slot count. With the example values above, ACP may run
up to eight cheap Luna jobs when physical slots are free, while no more than two Sol-high
jobs fit the weighted budget. Slot assignment remains exclusive. If a running job
escalates to a heavier profile, ACP
atomically resizes its lease and waits when the larger lease would exceed the budget.

The separate `codex_five_hour_soft_limit_percent` setting has a legacy name. ACP
applies it to the primary rate-limit window reported by Codex, regardless of that
window's actual duration. Once the threshold is reached, the guardrail blocks every
Codex profile, including Luna; weighted concurrency does not bypass the rate-limit cap.

Managed Luna, Terra, and Sol profiles accept reasoning efforts `none`, `low`,
`medium`, `high`, and `xhigh`. ACP rejects any other effort for those profiles
before it creates a job record or launches a worker. Explicit custom model names remain
pass-through because their supported effort set is backend-defined.

## Core Concepts

A route is a named repository target. It declares the canonical repository path and the
branch that must be checked out before a job may use it.

A slot is a reusable worktree path tied to a route. Slots let agents edit in isolated
workspaces while your canonical checkout stays untouched. Keep the slot parent outside
this repository, preferably as a sibling directory.

A job is one delegated task. Each job gets a `job_id`, prompt path, log path, result path,
and SQLite record.

A plan is a durable dependency graph of logical tasks. A task may also carry a private
execution specification, allowing ACP to claim and start dependency-ready work in a
one-shot dispatch pass. Jobs may be retried with new physical task IDs while remaining
attached to the same logical plan task. Snapshots expose only a brief hash/length,
progress, root-acceptance state, compact result excerpts, and a monotonic event cursor;
they never return the full execution brief.

The review inbox is the durable handoff boundary for terminal jobs and completed Codex
subagents. List operations return bounded excerpts and summaries. `inbox show` joins the
separate durable payload containing the full result, SHA-256 identity, structured
verification, rollout path, checkpoint identity, and root-review state. Delivery still
does not imply plan acceptance.

A backend is the runner implementation. Use `codex` for `codex exec` and `agy` for the
Antigravity CLI.

For Codex, workspace access is separate from the backend:

- `ide_mcp` is the default and preserves the existing AgentBridge contract, IDEA
  diagnostics/refactors, IDE Git operations, and terminal scoping.
- `native` uses Codex-native shell, `rg`, and file-edit tools in the exact assigned
  workspace. It skips IDEA module provisioning and disables the route-selected and
  known AgentBridge servers, reducing prompt and MCP-call overhead.

Set `workspace_access` globally, override it on a route, or pass
`--workspace-access`; job override wins over route, then global default. Native mode is
Codex-only, so ACP rejects `backend = "agy"` plus `workspace_access = "native"`.
The trade-off is deliberate: native mode has no IDEA diagnostics/refactors, and safe
Codex `workspace-write` protects `.git`, so the root reviewer normally creates the
commit after reviewing the diff. `--yolo` can remove that protection but is not the
default or recommended workflow.

Set `agy_model` in `[control.defaults]` or on a route when Antigravity must use an
explicit model. A job-level `--agy-model` override has highest priority. ACP persists
the resolved model and passes it to `agy --model`, so quota failures and imported
usage can be attributed to the model that actually ran.

For AGY jobs that must edit managed slots outside the open IDEA base directory, set
`agy_mcp_server` on the route to the AgentBridge server name from Antigravity's
`User/mcp.json` (for example, `agentbridge-ide`). ACP then requires exact workspace
paths through AgentBridge and forbids junction/symlink aliases. Without this setting,
AGY keeps the legacy native `idea` MCP contract for compatibility.

## Slot Workflow

Create or synchronize slots after editing the config:

```powershell
agent-control slots sync --config .\config\workspaces.toml
agent-control slots list --config .\config\workspaces.toml
agent-control slots create app-1 --config .\config\workspaces.toml
agent-control slots ensure-root-module --remove-slot-modules --config .\config\workspaces.toml
```

IDE module commands update `.idea/modules.xml` and `.idea/workspace.xml`; they cannot
query the live IDE runtime. Their JSON distinguishes `workspace_configured_loaded` from
`runtime_loaded` (reported as unknown) and sets `ide_reload_required` when files changed.
Reload the IDEA project/model before relying on a newly written module. In `ide_mcp`
jobs, every terminal command also performs an explicit `Set-Location`/`cd` to the
assigned workspace because the IDE terminal working-directory argument is not reliable
on every host.

For a new route or a new slot, `bootstrap` can add the missing config and optionally
create the worktree:

```powershell
agent-control slots bootstrap app-2 `
  --route app `
  --repo-path C:\path\to\my-project `
  --config .\config\workspaces.toml
```

Check out a clean inactive slot to a task branch:

```powershell
agent-control slots checkout app-1 `
  --branch codex/my-task `
  --start-point origin/main `
  --config .\config\workspaces.toml
```

Cleanup is dry-run unless `--apply` is passed:

```powershell
agent-control slots cleanup --max-per-route 2 --config .\config\workspaces.toml
agent-control slots cleanup --max-per-route 2 --apply --config .\config\workspaces.toml
```

With `terminal_slot_policy = "checkpoint"`, a dirty terminal job is captured as a local
commit under `refs/agent-control-plane/jobs/<job-hash>`. ACP verifies that ref and writes
the review-inbox item before resetting the slot to its unchanged branch `HEAD`. It never
pushes, merges, moves the branch, or accepts the work. If the workspace changes after the
checkpoint or verification fails, cleanup is refused and the slot remains dirty.

Use the manual command for a terminal job that predates this policy:

```powershell
agent-control slots checkpoint app-1 `
  --job-id <terminal-job-id> `
  --config .\config\workspaces.toml
```

Inspect pending job and Codex-subagent deliveries without replaying their full logs:

```powershell
agent-control inbox sync-subagents --config .\config\workspaces.toml
agent-control inbox list --sync-subagents --parent-thread-id <root-thread-id> --config .\config\workspaces.toml
agent-control inbox show agent_job:<job-id> --config .\config\workspaces.toml
agent-control inbox resolve agent_job:<job-id> --decision accepted --config .\config\workspaces.toml
```

Inbox resolution records root review only. For a plan-bound job, run `plan accept`
separately to unlock dependent tasks. Parent-thread filtering keeps a resumed root task
from loading unrelated subagent results that happen to share the same repository.
Normal acceptance requires a valid, review-ready `verification.json`; missing, malformed,
or status-mismatched verification remains visible but cannot be accepted as verified work.

## Plan Supervisor Workflow

The plan supervisor includes a durable one-shot dispatcher, not a background daemon.
Each `dispatch` call atomically claims up to `--max-jobs` ready tasks, materializes their
private briefs, and starts jobs through the existing policy/slot runner. Concurrent
dispatchers cannot claim the same task. A dispatch or worker failure is durable and is
never retried automatically; the root must request `retry` explicitly.

Create a whole plan in one call from a JSON manifest:

```json
{
  "plan_id": "main-to-dev-transfer",
  "title": "Restore main to dev parity",
  "objective": "Transfer and verify the remaining product behavior",
  "tasks": [
    {
      "task_id": "schema",
      "title": "Transfer schema",
      "execution": {
        "route": "app",
        "slot": "app-1",
        "backend": "codex",
        "workspace_access": "native",
        "codex_quality_tier": "mechanical",
        "brief": "Transfer only the bounded schema behavior and run focused checks."
      }
    },
    {"task_id": "api", "title": "Transfer API", "depends_on": ["schema"]}
  ]
}
```

```powershell
agent-control plan create --manifest .\transfer-plan.json --config .\config\workspaces.toml
agent-control plan summary main-to-dev-transfer --config .\config\workspaces.toml
agent-control plan dispatch main-to-dev-transfer --max-jobs 2 --config .\config\workspaces.toml
```

Only tasks with an `execution` object are dispatchable. Tasks without one remain valid
for manual `start --plan-id --plan-task-id` binding. After a failed attempt, optionally
replace the private brief and explicitly retry before the next dispatch:

```powershell
agent-control plan retry main-to-dev-transfer schema `
  --brief-file .\schema-repair.md `
  --config .\config\workspaces.toml
agent-control plan dispatch main-to-dev-transfer --max-jobs 1 --config .\config\workspaces.toml
```

Bind a worker launch directly to a logical task. A retry can use a new job task ID while
keeping `--plan-task-id` stable:

```powershell
agent-control start `
  --task-id schema-repair-r2 `
  --plan-id main-to-dev-transfer `
  --plan-task-id schema `
  --route app `
  --slot app-1 `
  --config .\config\workspaces.toml
```

A successful, fully finalized worker result enters `awaiting_review`; it does not unlock
dependants until the root agent records acceptance. The Markdown result status terminates
the worker even if structured verification is missing, so a malformed handoff cannot keep
a slot alive forever. It still cannot pass normal acceptance:

```powershell
agent-control plan accept main-to-dev-transfer schema `
  --sha <accepted-commit> `
  --config .\config\workspaces.toml
```

`plan accept` is the dependency-gate decision that unlocks dependants. It is separate
from `review attach`, which records root-review token/time accounting; use both when you
want both orchestration state and economic attribution.

The first summary returns the current projection and a `cursor`. Pass that cursor back to
receive only new entries in `changes`; current state remains available through the
bounded state lists, `completed_tasks`, `item_counts`, and `truncated` metadata. This
keeps blockers, review requests, running work, completed identities, and ready tasks
visible without replaying full logs:

```powershell
agent-control plan summary main-to-dev-transfer `
  --since <cursor> `
  --event-limit 100 `
  --item-limit 20 `
  --config .\config\workspaces.toml
```

Use `plan watch` with the returned cursor to let ACP monitor SQLite and return only when
the plan changes, instead of making the coordinating agent poll every job:

```powershell
agent-control plan watch main-to-dev-transfer `
  --since <cursor> `
  --timeout-sec 25 `
  --config .\config\workspaces.toml
```

Full logs and result files remain in their normal run artifacts. Plan snapshots include a
bounded result excerpt but never embed worker logs, keeping the main-agent context small.

Every writable worker is instructed to create a sibling `verification.json` with this
versioned shape:

```json
{
  "schema_version": 1,
  "status": "completed",
  "changed_files": [{"path": "src/app.py", "change": "modified"}],
  "checks": [{
    "command": "pytest -q tests/test_app.py",
    "cwd": ".",
    "outcome": "passed",
    "exit_code": 0,
    "summary": "3 passed"
  }],
  "unverified": []
}
```

ACP validates the schema and status match, hashes the canonical payload, and compares its
changed-file claims with the verified checkpoint tree. Worker claims remain explicitly
untrusted until root review.

## Start A Job

Run a supervised Codex job in a slot:

```powershell
agent-control start `
  --config .\config\workspaces.toml `
  --task-id fix-login-validation `
  --route app `
  --slot app-1 `
  --expected-branch codex/my-task `
  --wait `
  --live `
  --poll-interval-sec 30 `
  --lines 120
```

Run the same class of job without AgentBridge/IDEA:

```powershell
agent-control start `
  --config .\config\workspaces.toml `
  --task-id fix-login-validation-native `
  --route app `
  --slot app-1 `
  --expected-branch codex/my-task `
  --workspace-access native `
  --wait `
  --live
```

Run a read-only review job:

```powershell
agent-control start `
  --config .\config\workspaces.toml `
  --task-id review-auth-flow `
  --route app `
  --slot app-1 `
  --expected-branch codex/review-auth-flow `
  --read-only `
  --wait `
  --live
```

Override backend or model per job when needed:

```powershell
agent-control start `
  --config .\config\workspaces.toml `
  --task-id agy-canary `
  --route app `
  --slot app-1 `
  --backend agy `
  --wait `
  --live
```

If you enable automatic Antigravity account switching with token refresh, keep OAuth
client credentials outside the repository and set them in the local environment:
`AGENT_CONTROL_PLANE_OAUTH_CLIENT_ID`, `AGENT_CONTROL_PLANE_OAUTH_CLIENT_SECRET`, and
optionally `AGENT_CONTROL_PLANE_OAUTH_CLIENT_KEY`.

## Inspect Jobs

```powershell
agent-control list --config .\config\workspaces.toml --limit 20
agent-control status <job-id> --config .\config\workspaces.toml
agent-control summary <job-id> --config .\config\workspaces.toml --lines 40
agent-control analytics --config .\config\workspaces.toml --model gpt-5.6-terra --valid-only
agent-control watch <job-id> --config .\config\workspaces.toml --live --lines 120
agent-control tail <job-id> --config .\config\workspaces.toml --lines 120
agent-control result <job-id> --config .\config\workspaces.toml
agent-control cancel <job-id> --config .\config\workspaces.toml
```

Codex attempts keep raw `attempt-*.events.jsonl` streams and persist duration, token,
cache-hit, tool-call, result-status, failure, and estimated credit/API-cost metrics in
SQLite. `status` and `summary` include the latest attempt; `analytics` aggregates
comparable model/effort runs.

Root review is accounted separately so agent savings are not overstated. Start from the
current Codex rollout or import an existing marker, checkpoint phase boundaries, attach
the acceptance outcome for each job, and finish the span:

```powershell
agent-control review start --config .\config\workspaces.toml --name transfer --session <rollout.jsonl>
agent-control review checkpoint <span-id> review --config .\config\workspaces.toml
agent-control review attach <span-id> <job-id> accepted --root-verified --accepted-sha <sha> --config .\config\workspaces.toml
agent-control review finish <span-id> --config .\config\workspaces.toml
agent-control review show <span-id> --config .\config\workspaces.toml
```

The report uses `uncached input + output` as comparable tokens and publishes
`review_tax = root comparable / accepted agent comparable`. Codex attempts also have
hard tool-call budgets per quality tier (45 mechanical, 80 balanced, 120 deep by
default); `start --codex-tool-call-budget` is the explicit override. In `ide_mcp`,
terminal MCP calls must use the exact task ID as their tab name. In `native`, started
command and file-change events count toward the same budget without an IDE tab.

Archive old terminal runs:

```powershell
agent-control archive --older-than-days 14 --limit 50 --config .\config\workspaces.toml
agent-control archive --older-than-days 14 --limit 50 --apply --config .\config\workspaces.toml
```

## Codex MCP Server

Install with the `mcp` extra and copy `docs/codex-mcp.example.toml` into your Codex
`config.toml`, replacing the placeholder paths with your clone path. Use the clone's
absolute `.venv\Scripts\python.exe`, keep the MCP tool timeout longer than the watch
window, and restart Codex after changing the MCP block.

The MCP server exposes tools such as:

- `agent_start_job`
- `agent_watch_job`
- `agent_status_job`
- `agent_summary_job`
- `agent_analytics`
- `agent_plan_create`
- `agent_plan_add_task`
- `agent_plan_bind_job`
- `agent_plan_snapshot`
- `agent_plan_watch`
- `agent_plan_accept_task`
- `agent_plan_reject_task`
- `agent_plan_dispatch`
- `agent_plan_retry_task`
- `agent_plan_list`
- `agent_review_inbox_list`
- `agent_review_inbox_get`
- `agent_review_inbox_resolve`
- `agent_sync_subagent_results`
- `agent_tail_job`
- `agent_result_job`
- `agent_cancel_job`
- `agent_archive_jobs`
- `agent_smoke`
- `agent_reconcile`
- `agent_slots_sync`
- `agent_slots_list`
- `agent_slots_bootstrap`
- `agent_slots_create`
- `agent_slots_checkout`
- `agent_slots_checkpoint`
- `agent_slots_cleanup`

When Codex delegates through this control plane, the handoff should be supervised:
start with `--wait`, call `watch`, or set up external monitoring before ending the turn.
Treat `queued` and `running` as in-progress states, not successful completion.

## Safety Model

- `allow_dirty = false` refuses dirty workspaces by default.
- `--read-only` records a baseline and marks the job as `guardrail_violation` if files
  change.
- `yolo = false` keeps `--dangerously-bypass-approvals-and-sandbox` disabled unless a
  caller passes an explicit `--yolo` override.
- Slot assignment is exclusive while a slot has an active job.
- Ordinary slot inspection is read-only; only explicit lifecycle/reconciliation paths may
  change ownership or make a preserved slot available again.
- Worker identity, heartbeat, finalization intent, and finalization outcome are durable.
  A stale PID alone is never trusted as worker ownership.
- Failed agent runs are not restarted over dirty workspaces by default.
- Terminal slot cleanup happens only after a controller ref and inbox record are durable;
  concurrent late edits or unverifiable refs leave the slot dirty.
- `runs/`, `.agent-work/`, local SQLite databases, and `config/workspaces.toml` are
  ignored because they can contain private prompts, logs, paths, and repository state.
- The example keeps slots and generated worktrees outside this repository; legacy in-repo
  `.slots/` and `.worktrees/` folders are also ignored if they exist locally.

## Project Status

This is an alpha local-automation tool. Keep configs private, review generated patches
before merging them, and start with read-only jobs until your route and slot setup is
proven on your machine.
