# 0.1.x compatibility contract

This is the public contract for the 0.1.x alpha line. It describes supported behavior,
not a promise that every future feature will remain alpha-stable.

## Supported surface

- Python 3.11 and 3.12 are supported by package metadata and CI. Python 3.13+,
  older versions, and alternative implementations are not promised.
- Windows is best-tested, especially for the AGY PTY runner. Linux is supported for the
  plain subprocess Codex backend and CI checks. macOS is not a 0.1.x target.
- The agent-control console script and config/workspaces.example.toml are public entry points.
- SQLite is the durable local state store. ACP owns its configured database; it does
  not migrate an external Antigravity account database.

## Promises and alpha limits

Within 0.1.x, maintainers should preserve documented CLI command names, options, TOML
keys, and accepted-value meanings. Additive changes are preferred. Invalid
configuration must fail closed.

The promise covers the codex, agy, and claude (Claude Code CLI; `claude-code` is a
legacy alias) backends, subject to the installed external CLI. `native` uses native
shell/search/edit tools; `ide_mcp` drives the workspace through the route's
IDEA/AgentBridge MCP server. All three backends support `ide_mcp`, and codex/claude also
support `native` (agy is `ide_mcp` only). For a claude `ide_mcp` job, ACP hands the
worker exactly that one route server via a per-job `--mcp-config`, sourced from a
`[control.claude_mcp_servers]` override or, by default, the operator's Claude config;
`claude_bare` isolation still keeps every other operator MCP server out of the worker. A
claude `ide_mcp` job whose server cannot be resolved fails closed at launch. Agents stay
inside the declared route and slot workspace. ACP does not promise arbitrary IDE
modules or external workspace paths.

The alpha line does not promise a stable plugin API, database schema for direct
third-party writes, log text format, internal Python imports, or automatic downgrades.
A 0.1.x update may require a forward migration. Root review remains mandatory:
delivery, checkpoint, or successful worker verification never accepts, merges, pushes,
or publishes work.

## Durable artifacts and SQLite

Jobs persist IDs, prompts, logs, results, process identity, and state in SQLite and
under runs/. Plans persist dependencies and bounded snapshots. The review inbox stores
a hashed full result, structured verification, checkpoint identity, and root review
state. These are local artifacts, not a hosted service or remote backup. Back up runs/
and the configured database before upgrades.

SQLite bootstrap enables WAL and creates schema_migrations; migrations are versioned
per component and protected by checksums. Never edit migration rows manually. A
checksum mismatch is an upgrade blocker.
