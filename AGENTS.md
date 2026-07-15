# agent-control-plane Development Rules

- Keep this project independent of configured target repositories. It controls jobs; it does not contain task work.
- Do not edit target repositories from this project. The runner must keep every delegated agent inside the declared task workspace. `workspace_access = "ide_mcp"` uses the selected IDEA MCP; `workspace_access = "native"` uses Codex-native shell/search/file tools and must not depend on AgentBridge.
- Preserve user changes. Refuse dirty task workspaces by default and record blockers instead of switching branches or cleaning files.
- When `terminal_slot_policy = "checkpoint"`, clean terminal task changes only after the
  controller-owned Git ref and review-inbox record are verified durable. Never move the
  slot branch, push, merge, or treat checkpoint creation as root acceptance. Fail closed
  on late edits or verification errors.
- Do not run dependency installation commands from job execution. Missing dependencies are a task result blocker.
- Do not enable `--dangerously-bypass-approvals-and-sandbox` by default. A job may use yolo mode only when the caller passes an explicit option.
- Keep job state durable in SQLite and run artifacts under `runs/<job-id>/`.
- Stop only the exact worker or runner PID recorded for the current job. Never stop processes by name.
- Codex-facing job starts must support supervised completion: use `start --wait`,
  `watch`, or heartbeat monitoring instead of ending a handoff while a job is only
  `queued` or `running`.
- Represent multi-job epics as durable plans. Coordinating agents should use plan
  snapshots/watch cursors and bounded result excerpts instead of replaying full worker
  logs. Executable tasks must be claimed through the one-shot plan dispatcher; dispatch
  and worker failures require explicit retry. Dependent tasks remain locked until root
  acceptance is recorded.
- A durable result must contain a plain-line `Status: completed`, `Status: partial`, or `Status: blocked` marker. Inline-code status is tolerated for recovery, but generated prompts should use the plain form.
- Writable workers must also produce schema-v1 `verification.json`. Missing or invalid
  verification may terminate a worker, but it must block normal root acceptance.
- In `ide_mcp` mode, isolate simultaneously loaded checkouts that share Python/TypeScript namespaces: a route with `ide_sdk_name` gets one IDEA module per slot, using the exact installed SDK and no cross-slot module dependencies.
- In `ide_mcp` mode, use `agentbridge-slots-root` only for routes without a dedicated SDK and without overlapping package namespaces. Configure duplicate analysis as `SAME_MODULE` so useful intra-slot findings remain enabled while branch-clone matches are excluded.
- In safe native `workspace-write`, treat `.git` as protected: inspect status/diffs but leave commits to the root reviewer unless yolo was explicitly authorized.
