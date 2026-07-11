# agent-control-plane Development Rules

- Keep this project independent of configured target repositories. It controls jobs; it does not contain task work.
- Do not edit target repositories from this project. The runner must instruct the delegated agent to use IDEA MCP tools inside the declared task workspace.
- Preserve user changes. Refuse dirty task workspaces by default and record blockers instead of switching branches or cleaning files.
- Do not run dependency installation commands from job execution. Missing dependencies are a task result blocker.
- Do not enable `--dangerously-skip-permissions` by default. A job may use yolo mode only when the caller passes an explicit option.
- Keep job state durable in SQLite and run artifacts under `runs/<job-id>/`.
- Stop only the exact worker or runner PID recorded for the current job. Never stop processes by name.
- Codex-facing job starts must support supervised completion: use `start --wait`,
  `watch`, or heartbeat monitoring instead of ending a handoff while a job is only
  `queued` or `running`.
- Isolate simultaneously loaded checkouts that share Python/TypeScript namespaces: a route with `ide_sdk_name` gets one IDEA module per slot, using the exact installed SDK and no cross-slot module dependencies.
- Use `agentbridge-slots-root` only for routes without a dedicated SDK and without overlapping package namespaces. Configure duplicate analysis as `SAME_MODULE` so useful intra-slot findings remain enabled while branch-clone matches are excluded.
