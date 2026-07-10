# Codex Worker Routing

## Default

Use `gpt-5.6-terra` with `model_reasoning_effort = "medium"` for delegated coding
workers. Terra is the balanced capability/cost tier, and OpenAI documents `medium` as
the balanced starting point for most agents.

Use overrides intentionally:

- `gpt-5.6-luna` + `low`: mechanical edits, formatting, repetitive test updates, or
  high-volume read-only extraction with an explicit acceptance test.
- `gpt-5.6-terra` + `low`: latency-sensitive, already-specified work where the worker
  does not need to resolve architectural ambiguity.
- `gpt-5.6-terra` + `medium`: normal implementation, debugging, and bounded refactors.
- `gpt-5.6-terra` + `high`: complex edge cases or a second-opinion review when Sol is
  unnecessary.
- `gpt-5.6-sol` + `max`: final owner review, ambiguous architecture, security-sensitive
  work, or recovery after cheaper workers repeatedly fail.

Do not use `ultra` inside this control plane. Ultra can delegate internally, while this
runner already owns external slot fan-out; combining both makes attribution, cost, and
workspace ownership harder to control.

## Why Medium

Public coding benchmarks compare model tiers, not this repository's exact prompts and
effort settings. They justify Terra as the worker tier, but they do not prove that
`medium` beats `low` for every task. OpenAI's migration guidance explicitly recommends
testing the current effort and one level lower on representative tasks. The control
plane therefore records enough data for a local decision instead of treating a public
benchmark as an effort benchmark.

Compare identical tasks on clean slots at the same commit. Keep the prompt, timeout,
tools, acceptance criteria, and target files fixed. Evaluate:

1. Acceptance checks and reviewer findings.
2. Completion and retry rate.
3. Wall-clock duration.
4. Input, cached input, output, and reasoning tokens.
5. Result status (`completed`, `partial`, or `blocked`) plus reviewer findings.
6. Tool calls, failed tool calls, and error events.
7. Estimated Codex credits and API-equivalent cost.

Do not select `low` only because it is faster. Promote it for a task class only when it
matches `medium` on the acceptance rubric over multiple representative runs.

## Local Canary (2026-07-10)

Three read-only HH vacancy-state audits ran on commit `38639981` with identical
acceptance criteria and clean AgentBridge slots:

| Effort | Duration | Input / cached | Output / reasoning | Tools | Credits |
| --- | ---: | ---: | ---: | ---: | ---: |
| low | 223.2 s | 828,161 / 567,808 | 4,537 / 1,329 | 28 | 21.52 |
| medium | 221.7 s | 1,046,168 / 945,664 | 5,282 / 1,716 | 25 | 14.17 |
| high | 309.8 s | 900,926 / 725,504 | 8,692 / 4,851 | 21 | 18.76 |

All three completed without tool failures. Medium traced the full pipeline and found the
same material risk classes as high. Low was not faster in this sample and its report was
slightly less complete around the outer CLI entry point. This is a single canary per effort,
not a statistical benchmark; keep collecting representative runs through `--valid-only`.
It is enough to retain Terra medium as the default and require measured evidence before
promoting a task class to low or high.

## Telemetry

Each Codex attempt writes a raw `attempt-*.events.jsonl` stream next to the human log.
The runner persists attempt metrics in SQLite, including the result status used for the
reported success rate, and exposes them through:

```powershell
agent-control analytics --config .\config\workspaces.toml
agent-control analytics --config .\config\workspaces.toml --model gpt-5.6-terra --reasoning-effort medium --valid-only
```

Credit estimates use the Codex per-million-token rate card dated 2026-07-09. API cost
estimates use the GPT-5.6 API prices and the documented 90% cached-input discount. Raw
token counts remain authoritative if pricing changes.

## Sources

- [Codex subagent model and reasoning guidance](https://learn.chatgpt.com/docs/agent-configuration/subagents#choosing-models-and-reasoning)
- [GPT-5.6 migration and effort guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Codex token credit rate card](https://learn.chatgpt.com/docs/pricing#what-are-tokens-and-credits)
- [GPT-5.6 launch benchmarks and API pricing](https://openai.com/index/gpt-5-6/)
