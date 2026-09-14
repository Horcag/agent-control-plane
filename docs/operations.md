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

### AGY launcher and project-wrapper migration

`[control].agy_launch_mode` has two explicit states. `unmanaged` is the compatible default:
ACP preserves the operator's configured `agy_command` (including existing Windows/no-proxy
installations) and makes no claim that an adapter mediated the launch. `managed` requires an
explicit absolute, regular, non-symlink `[control].agy_proxy_launcher`; ACP validates it at
launch and fails closed when it is missing or invalid. Managed mode never falls back to the
real AGY command, and it adds `--new-project` once at the typed PTY launch boundary. The adapter
path and digest, mode, job, and attempt reference are recorded in a versioned launch receipt;
the receipt's local launch identifier is not a conversation identifier or CONNECT proof.

`agy_command` remains authoritative for unmanaged mode. Do not use `PATH` order as an adapter
selection policy. A managed configuration must name its adapter directly; a project wrapper is
not authority for adapter selection. ACP currently does not negotiate proxy capabilities, enforce
OS egress, prove CONNECT/account affinity, provide failover, or validate vendor updates. Those
runtime guarantees require a separate ADR and implementation.

For an existing unmanaged configuration, transition the config first. The default is a dry run;
the command binds the configured route/project, exact config bytes, existing `agy_command`, and
an absolute executable non-symlink adapter. It changes only `agy_launch_mode` and
`agy_proxy_launcher`, preserves the rest of the TOML text, and prints old/new hashes plus a
same-directory backup path. The config and each wrapper are deliberately separate single-file
transactions: if the later wrapper step fails, ACP still selects the configured managed adapter
for new launches and does not claim a cross-file rollback.

```sh
sha256sum /acp/.agent-work/workspaces.toml
agent-control agy-wrapper configure --config /acp/.agent-work/workspaces.toml \
  --project /project --expected-config-sha256 <unmanaged-config-sha256> \
  --expected-agy-command 'agy' \
  --adapter /home/nikit/.local/libexec/antigravity-proxy/agy
agent-control agy-wrapper configure --config /acp/.agent-work/workspaces.toml \
  --project /project --expected-config-sha256 <unmanaged-config-sha256> \
  --expected-agy-command 'agy' --adapter /home/nikit/.local/libexec/antigravity-proxy/agy --apply
```

Use the `after_sha256` printed by `configure` for the subsequent wrapper dry run and apply. Only
the exact historical wrapper or a complete versioned ACP wrapper is accepted; a launcher equal to
the wrapper, symlink inputs, symlink parent traversal, stale bytes/modes, or config drift is
rejected. The config binding is rechecked under the short-lived config-operation lock immediately
before wrapper publication and idempotent success.

```sh
sha256sum /project/.agent-work/bin/agy-project
agent-control agy-wrapper migrate --config /acp/.agent-work/workspaces.toml \
  --project /project --expected-sha256 <legacy-wrapper-sha256> \
  --expected-config-sha256 <config-sha256> \
  --expected-agy-command 'agy' \
  --launch-mode managed --adapter /home/nikit/.local/libexec/antigravity-proxy/agy
agent-control agy-wrapper migrate --config /acp/.agent-work/workspaces.toml \
  --project /project --expected-sha256 <legacy-wrapper-sha256> \
  --expected-config-sha256 <config-sha256> --expected-agy-command 'agy' \
  --launch-mode managed --adapter /home/nikit/.local/libexec/antigravity-proxy/agy --apply
```

For a fresh already configured project, use `generate` instead of a historical one-off script.
It only accepts a missing wrapper or already exact ACP-owned content, and it creates the owned
wrapper parent after the same config recheck:

```sh
agent-control agy-wrapper generate --config /acp/.agent-work/workspaces.toml \
  --project /fresh-project --expected-config-sha256 <config-sha256> \
  --expected-agy-command 'agy'
agent-control agy-wrapper generate --config /acp/.agent-work/workspaces.toml \
  --project /fresh-project --expected-config-sha256 <config-sha256> \
  --expected-agy-command 'agy' --apply
```

Each receipt contains `before_sha256`, `after_sha256`, `backup_path`, target mode/adapter, and
recovery text without printing config content. To recover, use the printed backup and post-write
hash; rollback and config restore reject symlinks, changed files, and mismatched hashes rather
than overwriting a late edit:

```sh
agent-control agy-wrapper rollback --config /acp/.agent-work/workspaces.toml \
  --project /project --backup /project/.agent-work/bin/agy-project.acp-agy-v2-<hash>.bak \
  --expected-current-sha256 <post-write-sha256>
agent-control agy-wrapper restore-config --config /acp/.agent-work/workspaces.toml \
  --project /project --backup /acp/.agent-work/workspaces.toml.acp-agy-v2-<hash>.bak \
  --expected-current-sha256 <configured-config-sha256>
```

### Repository-local planes

For project work, keep the canonical config at `<repository>/.agent-work/workspaces.toml`.
Give each project its own coordination directory, runs directory, and SQLite database.
Shared account-quota accounting may remain global. Slot paths must be project-specific;
they may live outside the repository when that is the project's chosen layout.

Implicit discovery prefers the enclosing repository's canonical config over registered
scratch copies. A linked Git worktree without its own config uses the main checkout's
canonical config when available. Explicit `--config` remains available for historical jobs.
Run one MCP server per config and update each project's client URL when changing planes.

During migration, retain existing jobs, plans, reviews, and occupied slots in their original
database. Provision distinct slots for the new plane rather than copying live slot ownership.
Keep old artifact paths usable while historical records still reference them.

### Retiring a shared configuration

To redirect new jobs from a retired config while allowing jobs already recorded with it
to finish, add `<workspaces.toml>.retired.json` beside the old config:

```json
{
  "version": 1,
  "routes": {
    "arina": "/absolute/path/to/arina/.agent-work/workspaces.toml"
  }
}
```

By default the marker blocks every new start through that config. Each listed route
reports its replacement config; an unlisted route is refused until it is mapped. A
partially retired shared config may set `"allow_unlisted_routes": true`; then only the
listed routes are blocked. Invalid marker data fails closed before ACP creates a job,
acquires a slot, or starts a worker.

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
`start --wait` uses the same settled verdict and exit codes instead of returning success
when the launched job is blocked or finalization is still pending.

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

### MCP coordinators: watch parity and its limits

Use exact ACP tool names rather than broad tool discovery. Scope a call to its route, job,
or plan, consume only needed `structuredContent` fields (the authoritative payload), and
keep the returned cursor for the next plan/watch call. `content` is only a short summary.
Responses default to at most 48 KiB structured content and less than 64 KiB on the wire;
oversized fields are guarded with identity, counts, and a follow-up. Request `full=True`
only when a tool explicitly declares it. Do not re-request unchanged status or snapshot
data, and do not print whole tool responses.

```text
agent_plan_snapshot(plan_id="release") -> { cursor, ready_next, running, ... }
agent_plan_dispatch(plan_id="release", max_jobs=1) -> { dispatched, ... }
agent_plan_watch(plan_id="release", since=<cursor>) -> { cursor, changes, ... }
agent_summary_job(job_id="job-123", lines=20) -> compact review signal and bounded log tail
agent_result_job(job_id="job-123") -> bounded result preview plus hash and next_offset
agent_result_job(job_id="job-123", offset=<next_offset>) -> next bounded preview
agent_result_job(job_id="job-123", full=True) -> explicit unbounded legacy result
```

Keep expensive command output in the run artifact or worker log; report the exit code and
a short tail/count. Run independent checks separately (or with fail-fast semantics), so a
later success cannot hide an earlier failure. Use one combined `pytest -k 'api or cli'`
expression, not repeated `-k` flags. On WSL, fix the process environment once if
`python -c "import tempfile; print(tempfile.gettempdir())"` resolves under `/mnt`: native
Linux tools should receive `TMPDIR=/tmp` from the shell or Codex environment policy. Do
not hide a broken cross-filesystem temp configuration by prefixing individual test commands.

The CLI `watch --events` *streams*: a shell process holds a connection open and prints
lines as they arrive. MCP has no equivalent streaming transport, so that exact shape
cannot be mirrored. What MCP does have is `agent_watch_events`, the same event vocabulary
and the same selection (`job_ids`, `plan_id`, `task_glob`) delivered one non-blocking
pass at a time — see "Non-blocking event polling over MCP" below. A shell-capable root
agent should still prefer the CLI, because it gets push-shaped output and an exit code;
an MCP-only coordinator now has a first-class tool instead of a hand-rolled poll loop.

`agent_watch_job` and `agent_start_job(wait=True)` now carry the same settled/on-contract
verdict the CLI exit code encodes, reusing `is_settled`/`is_on_contract` from
`features/job_watch`:

- `settled`: `true` once the job's status is terminal **and** finalization has been
  decided (`completed`/`failed`, not `not_started`/`pending`). A job can be terminal
  before it is settled — finalization runs after the worker exits.
- `on_contract`: `null` while `settled` is `false` (the verdict is not yet decided, which
  is a different fact from "decided against"); once settled, `true` when the job's final
  `status` matches its declared `expected_result_status`, else `false` — the same rule
  the CLI's exit-code table above uses per job.

Every MCP long poll is capped at `_MAX_LONG_POLL_SEC` (300 s) regardless of the
`timeout_sec`/`wait_timeout_sec` argument passed; a longer value is silently clamped and
echoed back as `timeout_clamped_to`. That cap is not raised for this task and should not
be raised in general: it exists so one MCP call cannot hold a request open indefinitely.
Jobs run up to `timeout_sec = 3600` by default, so a single `agent_watch_job` call cannot
observe a whole job — in the worst case a coordinator needs a dozen sequential blocking
calls per job, each re-quoting `job_id`, to reach a settled verdict, and gets no push
notification in between.

### Non-blocking event polling over MCP

`agent_watch_events` takes one pass over a selection and returns immediately. Because it
never holds a call open, job duration no longer bounds what a coordinator can follow: a
60-minute job is as watchable as a 60-second one.

```text
agent_watch_events(job_ids=[...] | plan_id=... | task_glob=..., cursor=<previous cursor>)
  -> { ok, watched, events, cursor, done, pending, terminal_statuses }
```

- `events` are the same typed, deduplicated `start`/`transition`/`terminal`/`stale`/
  `resumed`/`watch_error` records the CLI prints, from the same `WatchEventStream`.
- `cursor` is opaque: pass the one you got back on the next call and you receive only what
  changed since. Omit it and you get a fresh baseline — chatty, never wrong.
- `done` is `true` once every watched job is *settled* (terminal status **and**
  finalization decided). It is the same predicate the CLI exit code uses, so a caller
  stops on `done` rather than on a retyped status list.
- `pending` lists each unsettled job with its last status, so a status that will never go
  terminal on its own — `cancel_requested` is the known case — is visible instead of
  silently waited on.
- `terminal_statuses` ships in every response; `agent_terminal_statuses` returns the same
  vocabulary standalone. A watcher must never retype this list.

**Why a cursor is honest here without a durable event log.** The dedup state a tick needs
is small and entirely per job: last status, whether it settled, and which heartbeat was
last reported stale. `export_cursor`/`load_cursor` hand that state to the caller instead
of keeping a `WatchEventStream` alive per selection inside the server. So the server stays
stateless — no unbounded session state, nothing a restart silently drops — while deltas
stay genuine rather than a full `START` snapshot on every call. A caller that loses its
cursor degrades to one extra baseline snapshot, not to a wrong verdict. Cursor entries for
jobs that have left the selection are dropped rather than re-added, so a stale cursor
cannot keep a watch pending on a job nobody follows any more. `CURSOR_VERSION` guards the
payload; a mismatch is a typed error, never a silent misread.

What MCP still does not get is *push*: the coordinator decides when to call again. That is
the remaining, deliberate asymmetry with the streaming CLI.

`agent_watch_job` and `agent_start_job(wait=True)` remain the right call for a single job
a coordinator wants to block on briefly. For several jobs, or any job that outlives one
call, use `agent_watch_events`. For plan tasks specifically, `agent_plan_snapshot`/
`agent_plan_watch` with the `since` cursor remain equivalent and are backed by the plan
event log.

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

A controller gate battery's report-level `status` is `passed`, `failed`, or
`timed_out`: a failed check always wins the roll-up (a confirmed defect must never
read as merely unresolved), and `timed_out` only appears when nothing failed but at
least one gate ran out of its configured `timeout_sec`. Both `failed` and `timed_out`
block `review_ready` identically -- neither can substitute for the other kind of
evidence -- but the review-inbox item's `verification_bundle.review_blocked_reason`
(also surfaced in `inbox list`'s `verification_summary`) names which one happened and
which gates were involved, so a reviewer does not have to open `native-quality.json`
to tell "we did not find out" from "we found a problem".

`native_quality_max_parallel` (per route) only bounds gate processes *within* one
job's battery. `[control.defaults] native_quality_global_max_parallel` (default `4`)
bounds them *across* jobs finalizing at the same time, using the same cross-process
SQLite lease pattern as the Codex quota broker (`GlobalQuotaBroker`) rather than an
in-process semaphore, since finalization can happen in separate processes. A gate that
cannot obtain a slot before its own `timeout_sec` elapses is recorded as `timed_out`
rather than run past its budget or bypass the bound.

After coordinator loss, run ordinary reconciliation first:

```powershell
agent-control reconcile --config .\config\workspaces.toml
agent-control reconcile --job-id <job-id> --terminate-verified-runners --config .\config\workspaces.toml
```

`reconcile` exits `0` only when it reports no recovery errors or unresolved process
identity conflicts; it exits `1` when any such blocker remains. Read the JSON report
before retrying.

The termination option is opt-in and acts only when durable PID, start identity, and
executable still match. Missing identity, PID reuse, unsupported platform, or any
verification error leaves the runner and slot quarantined. Never kill by process name.

For retention, inspect first and apply only after review records are durable:

```powershell
agent-control archive --older-than-days 14 --limit 50 --config .\config\workspaces.toml
agent-control archive --older-than-days 14 --limit 50 --apply --config .\config\workspaces.toml
```

### Accepted slot lifecycle and audit

Configure `canonical_remote` and `canonical_branch` on every route eligible for
cleanup. Without both values ACP reports the route as quarantined. Audit refreshes
only the named branch and never deletes anything:

```bash
agent-control lifecycle audit --config .agent-work/workspaces.toml
agent-control lifecycle reconcile --config .agent-work/workspaces.toml
agent-control lifecycle poll --passes 3 --interval-sec 30 --max-interval-sec 300 \
  --config .agent-work/workspaces.toml
```

`reconcile` can enqueue an exact operation after acceptance and canonical
reachability are proved; it cannot apply it. Apply one inspected operation with
`agent-control lifecycle apply <operation-id>`. Apply revalidates generation,
path device/inode, tracked and untracked cleanliness, live process CWDs,
checkpoint ref/SHA, common Git directory, remote URL, and canonical tip. Any
mismatch is quarantined. Successful cleanup uses compare-and-delete refs, returns
a reusable slot to `slot/<name>` (or removes an owned temporary worktree), and
runs `git worktree prune`. The journal makes retry idempotent.

Acceptance triggers the same read-only refresh/enqueue pass. Bounded polling is
an exponential-backoff safety net and never applies cleanup itself.

Retention refuses checkpoint-ref deletion when the stored SHA differs. MCP exposes the
same durable plan, handoff, checkpoint, and reconciliation boundaries; use the CLI when
you need a copyable audit trail. SQLite runs in WAL mode with versioned migrations and
preserves orphan events. On contention, retry the operation after the writer commits;
do not delete or replace the database.

Safety baseline: refuse dirty task workspaces, keep target repositories independent of
ACP, preserve `.git` in native workspace-write, and inspect status/diff before review.

#### Unowned branch audit and legacy lifecycle forensics

During plane migrations or forensic cleanups, `agent-control lifecycle audit` also
evaluates unowned local Git branches across configured routes:

- **Multiple legacy database discovery**: ACP discovers and aggregates evidence across all
  distinct discovered legacy coordination databases (such as historical migration databases
  and coordination SQLite files). Discovered database paths are deduplicated and sorted
  deterministically.
- **Fail-closed schema safety**: Each candidate legacy database is inspected for schema
  compatibility. Corrupted, unreadable, or malformed databases fail closed; ACP records a
  blocker/quarantine rather than silently ignoring errors or authorizing deletion.
- **Strict repository-identity binding**: Legacy acceptance evidence is strictly bound to
  repository identity: an existing workspace must resolve to the exact target common Git
  directory. Non-existent or removed historical workspaces cannot prove identity by lexical
  containment alone; they strictly require an independently present exact or hashed job
  checkpoint ref in the target repository (and matching SHA where the schema supplies it).
  Route names, branch names, task IDs, and claimed paths alone are never trusted as proof
  of repository identity. Foreign databases without verified repository ownership retain
  branches as unowned and cannot authorize deletion.
- **Branch ownership derivation**: Branch ownership is derived exclusively from verified job
  records linking job identifiers to `jobs.expected_branch` and verified workspace paths.
  Dispatch identifiers such as `review_inbox_items.task_id` are never used as Git branch names.
- **Conservative classification**: Branches with unmerged work, foreign repository ownership,
  or ambiguous/contradictory records remain `retained_unowned` or `quarantined_unowned`.
  Only branches proven to be fully integrated into canonical tip or patch-equivalent with
  verified durable root acceptance are proposed for deletion. Patch equivalence fails closed:
  empty `git cherry` output for a non-ancestor candidate returns False; any merge commit
  in the candidate-only range disqualifies patch equivalence; and every candidate-only non-merge
  commit must affirmatively match a `- <sha>` line with zero `+` lines.

### Optional CLI-only Antigravity Manager switching (Windows and WSL)

The legacy `manager switch-agy` adapter writes the Windows credential store. For
current AGY CLI file authentication, opt into the CLI-only adapter with a local
`~/.config/agent-control-plane/manager-cli.json`, or point
`AGENT_CONTROL_PLANE_MANAGER_CLI_CONFIG` at another absolute config path:

```json
{
  "database_path": "/mnt/c/Users/USER/.antigravity-agent/cloud_accounts.db",
  "manager_user_data": "/mnt/c/Users/USER/AppData/Roaming/Antigravity Manager",
  "electron_command": ["/absolute/path/to/installed/electron.exe"],
  "helper_windows": true,
  "token_paths": [
    "/home/USER/.gemini/antigravity-cli/antigravity-oauth-token",
    "/mnt/c/Users/USER/.gemini/antigravity-cli/antigravity-oauth-token"
  ],
  "auto_switch_on_quota": true
}
```

Use native absolute Windows paths when the controller runs on Windows; UNC WSL
paths may also be explicit targets. `helper_windows` translates WSL paths for a
Windows Electron helper. The helper must run under the Windows user who owns the
Manager encryption key. No dependency is downloaded or installed by this adapter.
All target parents must already exist. No target repository configuration changes
are required, and absence of this local file preserves the existing behavior.

Disable Manager's **Auto-Switch** before applying CLI switches. The adapter refuses
application while it is enabled: Manager has no independent CLI-sync off switch
and would otherwise race the ACP writer. Manager can remain open for account and
quota inspection. A manual Manager switch is still an external writer; inspect
CLI identity again before continuing. Keep Manager's quota cache refreshed.

```sh
agent-control manager accounts --model gemini-3.8-flash-high
agent-control manager switch-agy --model gemini-3.8-flash-high
agent-control manager switch-agy --model gemini-3.8-flash-high --apply
```

The first switch command is a preview. Use `--account-id` for an explicit account.
MCP exposes `agent_agy_accounts(model)` and `agent_agy_switch(model, account_id,
apply=false)`. Both expose sanitized metadata only. CLI identity comes from the
actual token files, not the Manager active-target setting. The adapter never
writes IDE keyrings, Manager's active account, or generic Gemini CLI caches.

Selection uses the exact requested model's cached percentage and account status;
unknown quota is not zero or available. Cache freshness is explicitly unknown.
A stored zero whose reported reset has passed permits one bounded provider probe;
it is not presented as a refreshed positive quota. A rejected probe enters cooldown.
Quota failures are recorded in a shared, model-specific cooldown file until
the reported reset (or five minutes when absent). Concurrent ACP recovery can
reuse a peer's verified switch. Each job excludes accounts it already exhausted.
The selected account is remembered locally and restored before each new ACP attempt,
including when an older CLI process has rewritten its previous token during OAuth
refresh. Recovery preserves the model and existing progress; it does not accept results
or replay completed jobs. Jobs already running at installation retain their old
controller code; newly launched jobs load the integration.

The CLI credential format and atomic-file approach follow Antigravity Manager;
expiry timestamps in seconds and milliseconds are supported. AGY itself renews
expired access tokens using the saved refresh token. New credentials take effect
in new AGY processes; this does not promise hot account changes in an existing
CLI process. Writes use private temporary files, replacement, and read-back on
all configured targets. Multiple filesystems cannot form one atomic transaction:
a failed second write rolls back earlier writes when they still contain this
operation's bytes, otherwise the operation reports uncertainty without claiming
success. Never blindly replay a switch with uncertain completion.

ACP writers coordinate using atomic creation of a shared `acp-cli-switch.lock`
directory next to the Manager database. SQLite locks are not used for this purpose:
a live Windows/WSL test showed they do not safely coordinate this filesystem boundary.
The selected-account and cooldown metadata use atomic JSON replacement. A crashed
writer can leave a lock directory: acquisition times out with an explicit error.
Inspect its `owner.json` and verify that its owner has exited before removing it;
there is no time-based stale-lock takeover.
