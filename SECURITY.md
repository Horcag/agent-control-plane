# Security Policy

Agent Control Plane is local automation software. It starts agent processes, writes prompts and logs, and can run commands in configured repositories. Treat every configured workspace as trusted local state.

## Supported Versions

Only the current `main` branch is maintained before a tagged release exists.

## Sensitive Data

Do not commit `runs/`, `.agent-work/`, slot directories, generated worktrees, local SQLite databases, OAuth client secrets, account emails, credentials, or machine-specific `config/workspaces.toml` files. Run artifacts may contain prompts, paths, branch names, command output, and task results from private repositories.

## Reporting Issues

For now, open a GitHub issue with a minimal reproduction. Do not include secrets, private prompts, run logs, or private repository paths in public reports.
