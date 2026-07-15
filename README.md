# Agent Control Plane

Agent Control Plane is a local job orchestrator for delegating bounded coding tasks to
agent CLIs while preserving route, branch, slot, log, and result state.

It currently supports two runner backends:

- `codex`: runs `codex exec` in the selected workspace.
- `agy`: runs Google Antigravity CLI (`agy`) through a PTY-backed runner.

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
python -m pytest
```

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
allow_dirty = false
yolo = false
prepare_slots = true
runs_layout = "date"
codex_global_max_concurrent_jobs = 2
codex_global_max_burst_jobs = 4

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
model.

### Weighted Codex Concurrency

`codex_global_max_concurrent_jobs` is a weighted capacity budget measured in
Sol-high-equivalent slots, not a literal process limit. Each nominal concurrency unit
supplies 30 capacity units. Luna, Terra, and Sol jobs consume fewer or more units
according to the effective model and reasoning effort; unknown model names consume all
30 units.

`codex_global_max_burst_jobs` is the separate hard process-count limit and defaults to
twice the weighted slot count. With the example values above, ACP may run four cheap Luna
jobs when four physical slots are free, while no more than two Sol-high jobs fit. Slot
assignment remains exclusive. If a running job escalates to a heavier profile, ACP
atomically resizes its lease and waits when the larger lease would exceed the budget.

## Core Concepts

A route is a named repository target. It declares the canonical repository path and the
branch that must be checked out before a job may use it.

A slot is a reusable worktree path tied to a route. Slots let agents edit in isolated
workspaces while your canonical checkout stays untouched. Keep the slot parent outside
this repository, preferably as a sibling directory.

A job is one delegated task. Each job gets a `job_id`, prompt path, log path, result path,
and SQLite record.

A backend is the runner implementation. Use `codex` for `codex exec` and `agy` for the
Antigravity CLI.

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
default); `start --codex-tool-call-budget` is the explicit override. Terminal MCP calls
must use the exact task ID as their tab name, preventing cross-slot terminal reuse.

Archive old terminal runs:

```powershell
agent-control archive --older-than-days 14 --limit 50 --config .\config\workspaces.toml
agent-control archive --older-than-days 14 --limit 50 --apply --config .\config\workspaces.toml
```

## Codex MCP Server

Install with the `mcp` extra and copy `docs/codex-mcp.example.toml` into your Codex
`config.toml`, replacing the placeholder paths with your clone path.

The MCP server exposes tools such as:

- `agent_start_job`
- `agent_watch_job`
- `agent_status_job`
- `agent_summary_job`
- `agent_analytics`
- `agent_tail_job`
- `agent_result_job`
- `agent_cancel_job`
- `agent_archive_jobs`
- `agent_smoke`
- `agent_slots_sync`
- `agent_slots_list`
- `agent_slots_bootstrap`
- `agent_slots_create`
- `agent_slots_checkout`
- `agent_slots_cleanup`

When Codex delegates through this control plane, the handoff should be supervised:
start with `--wait`, call `watch`, or set up external monitoring before ending the turn.
Treat `queued` and `running` as in-progress states, not successful completion.

## Safety Model

- `allow_dirty = false` refuses dirty workspaces by default.
- `--read-only` records a baseline and marks the job as `guardrail_violation` if files
  change.
- `yolo = false` keeps `--dangerously-skip-permissions` disabled unless a caller passes an
  explicit `--yolo` override.
- Slot assignment is exclusive while a slot has an active job.
- Failed agent runs are not restarted over dirty workspaces by default.
- `runs/`, `.agent-work/`, local SQLite databases, and `config/workspaces.toml` are
  ignored because they can contain private prompts, logs, paths, and repository state.
- The example keeps slots and generated worktrees outside this repository; legacy in-repo
  `.slots/` and `.worktrees/` folders are also ignored if they exist locally.

## Project Status

This is an alpha local-automation tool. Keep configs private, review generated patches
before merging them, and start with read-only jobs until your route and slot setup is
proven on your machine.
