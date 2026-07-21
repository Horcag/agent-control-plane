# Claude Worker Routing

ACP drives Claude Code as a headless native backend (`claude -p --output-format
stream-json`). This document covers cheap-first model selection for `claude` jobs and
the knobs that control it. See `docs/codex-worker-routing.md` for the Codex-specific
routing, quota, and handoff mechanics, most of which apply unchanged to Claude jobs
(terminal handoff, checkpoint/slot release, worker-vs-controller quality gates,
`result.md`/`verification.json` bundle rules).

## Cheap-first ladder

Choose the least-capable profile that can plausibly complete the task, and escalate
only after a demonstrated failure — not preemptively for "hard-sounding" work:

- **Docs/config/comment-only edits, mechanical renames:** `claude-haiku-4-5` at
  low/medium effort, or `claude-sonnet-5` at low effort.
- **Ordinary implementation + tests (the default lane):** `claude-sonnet-5` at medium
  effort — this is the shipped `[control.defaults]` default. Use high effort for
  trickier work in the same lane.
- **Hard cross-cutting implementation or architecture-sensitive repair:** try
  `claude-sonnet-5` at xhigh effort first. Reach for `claude-opus-4-8` at high or
  xhigh effort only when a sonnet attempt already produced a wrong or partial result
  for capability reasons (not for timeouts, tooling errors, or missing context, which
  a bigger model won't fix).
- **`claude-fable-5` is not a worker-lane model.** Reserve it for root/coordinator
  phases that genuinely need frontier reasoning. It bills roughly 2x opus rates (see
  below), so an accidental worker-lane launch is expensive.

### Measured grounding (this host, 2026-07-20/21, API-equivalent USD)

Sticker API rates, verify before relying on them for billing — per MTok
(input / cache-read / output):

| Model | Input | Cache-read | Output |
| --- | --- | --- | --- |
| claude-haiku-4-5 | $1 | $0.10 | $5 |
| claude-sonnet-5 | $3 | $0.30 | $15 |
| claude-opus-4-8 | $5 | $0.50 | $25 |
| claude-fable-5 | $10 | $1.00 | $50 |

Cache writes bill above the base input rate: 1.25x input for a 5-minute TTL, or 2x
input for a 1-hour TTL (what Claude Code uses).

Measured worker jobs (agent-control-plane docs/fix tasks, `claude-sonnet-5` medium,
before worker isolation) cost $1.61-$2.01 per job, around 3-4M input tokens each with
97%+ cache hits. Worker isolation (`claude_bare = true`, see below) roughly halves
that by cutting per-request static context from ~90K to ~42K tokens.

Root/interactive sessions measured over 48h told a very different story: long
`claude-fable-5` sessions cost $84-$195 EACH (100M+ cache-read tokens at $1/M, 1h
cache writes at $20/M, output at $50/M) — far above any worker job. Cost scales with
session length times context size; model tier multiplies it on top (fable reads
roughly 2x opus, roughly 3.3x sonnet, at the same token count). Keep frontier models
out of the worker lane specifically because that multiplier compounds with job volume.

## Selecting models per job/route

The claude backend resolves a single fixed profile per job (no automatic
quality-tier ladder the way Codex has); precedence, most to least specific:

1. Job-level `--claude-model` / `--claude-reasoning-effort` on `start`, or the
   `claude_model` / `claude_reasoning_effort` keys passed to `agent_start_job` or a
   plan-task execution spec.
2. Route-level `claude_model` / `claude_reasoning_effort` in `[routes.<name>]`.
3. `[control.defaults] claude_model` / `claude_reasoning_effort` — shipped defaults
   are `claude_model = "default"` and `claude_reasoning_effort = "medium"`.

`--claude-model` and `--claude-reasoning-effort` are rejected outside the claude
backend; mixing them into a codex or agy launch is a launch-time error, not a
silent no-op.

### The `default` selector

`claude_model = "default"` does not mean "cheap" — it resolves to the
highest-priority visible model in the catalog, which is `claude-opus-4-8` in the
builtin inventory (priority 1, ahead of claude-sonnet-5 at priority 2). Operators who
want a cheap default must set `claude_model` explicitly (route-level or
`[control.defaults]`) rather than relying on `"default"`.

## Premium gating

Mark expensive models `premium = true` in a `[[control.claude_model_catalog.models]]`
overlay entry. An explicit launch of a premium model then requires a nonblank
`--codex-premium-override-reason` — the flag name is shared with the codex backend
(there is no separate `--claude-premium-override-reason`); a premium launch without
one fails before the job starts.

```toml
[[control.claude_model_catalog.models]]
model = "claude-opus-4-8"
premium = true
api_usd_rate = { input = 5.0, cached_input = 0.5, output = 25.0 }
rate_card_version = "2026-07-21"
rate_card_source = "operator-supplied example rates; verify before use"

[[control.claude_model_catalog.models]]
model = "claude-fable-5"
premium = true
api_usd_rate = { input = 10.0, cached_input = 1.0, output = 50.0 }
rate_card_version = "2026-07-21"
rate_card_source = "operator-supplied example rates; verify before use"
```

An overlay entry that omits `premium` defaults to `false`. A newly visible catalog
model with no overlay entry at all reports `premium = null` / `premium_state =
"unknown"` until an operator adds one — it is not treated as safe by default.

## Efforts

Per-model supported reasoning efforts, from the builtin catalog
(`src/agent_control_plane/features/agent_runner/lib/claude_model_catalog.py`):

| Model | Supported efforts |
| --- | --- |
| claude-opus-4-8 | low, medium, high, xhigh, max |
| claude-sonnet-5 | low, medium, high, xhigh, max |
| claude-fable-5 | low, medium, high, xhigh, max |
| claude-opus-4-7 | low, medium, high, xhigh, max |
| claude-opus-4-6 | low, medium, high, max (legacy set, no xhigh) |
| claude-sonnet-4-6 | low, medium, high, max (legacy set, no xhigh) |
| claude-haiku-4-5 | low, medium, high only |

Effort raises thinking depth and tool-call depth, not the per-token rate — the rate
table above is effort-independent. Cost impact from a higher effort comes through
more turns and more output tokens, not a different price per token, so bumping
effort on a long-running job can still cost meaningfully more even though the sticker
rate is unchanged.

## Worker isolation (`claude_bare`)

`[control.defaults] claude_bare` defaults to `true`. When set, ACP launches the
worker with `--strict-mcp-config --setting-sources project`, which keeps the
operator's MCP servers, plugins, and skills out of the worker process while leaving
the worker's own login/session intact. This is the source of the roughly 2x cost
reduction noted above (per-request static context drops from ~90K to ~42K tokens) —
it removes prompt overhead the worker never needed, not capability.

Token accounting for cost estimates follows the Anthropic usage fields directly:
`input_tokens` (ACP's total-input convention) sums uncached input, cache-read, and
cache-creation tokens; `cached_input_tokens` tracks cache-read tokens only;
`reasoning_output_tokens` is always 0 for Claude. When the Claude CLI reports
`total_cost_usd` for an attempt, ACP records it as `estimated_api_usd` with
`rate_card_version = "claude-code-cli"`; when the CLI doesn't report a cost, ACP
falls back to the configured `[[control.claude_model_catalog.models]]` rate card.
Raw token counts remain authoritative if pricing changes.

## Sources

- `src/agent_control_plane/features/agent_runner/lib/claude_model_catalog.py` (builtin
  catalog, priorities, supported efforts)
- `src/agent_control_plane/features/agent_runner/lib/job_launcher.py` (profile
  resolution and premium-gating checks)
- `src/agent_control_plane/shared/claude_session_usage.py` (token accounting)
- `src/agent_control_plane/features/agent_runner/lib/claude_telemetry.py`
  (`estimated_api_usd`, `claude-code-cli` rate card)
- `config/workspaces.example.toml` (annotated `claude_model_catalog` example)
- `docs/codex-worker-routing.md` (shared handoff, quota, and quality-gate mechanics)
