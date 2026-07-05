# agent-control-plane Development Rules

- Keep this project independent of configured target repositories. It controls jobs; it does not contain task work.
- Do not edit target repositories from this project. The runner must instruct the delegated agent to use AgentBridge/IDE tools inside the declared task workspace.
- Preserve user changes. Refuse dirty task workspaces by default and record blockers instead of switching branches or cleaning files.
- Do not run dependency installation commands from job execution. Missing dependencies are a task result blocker.
- Do not enable `--dangerously-skip-permissions` by default. A job may use yolo mode only when the caller passes an explicit option.
- Keep job state durable in SQLite and run artifacts under `runs/<job-id>/`.
- Stop only the exact worker or runner PID recorded for the current job. Never stop processes by name.
- Codex-facing job starts must support supervised completion: use `start --wait`,
  `watch`, or heartbeat monitoring instead of ending a handoff while a job is only
  `queued` or `running`.
- Keep AgentBridge-visible slot indexing centered on the single
  `agentbridge-slots-root` module for `slot_root`. Do not create a new IDEA module per
  task slot unless explicitly using the legacy rollback commands.
