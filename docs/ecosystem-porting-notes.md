# Ecosystem porting notes

Snapshot date: **2026-07-21**. Owner: ACP lead. Status: research → backlog (nothing ported yet).

## Purpose

A survey of the open-source "coding-agent orchestrator / control-plane" ecosystem as it stands
in mid-2026, filtered to the handful of projects that overlap ACP's *distinctive* layer
(durable control plane, DAG plans, verify-before-accept, checkpoint-not-merge), with a
prioritized list of concrete ideas/code worth porting **into** ACP and where each lands in
our FSD tree.

This is a **porting map, not a rewrite mandate.** ACP's core invariants are deliberate and
already ahead of most of the field on the orchestration axis; the value below is in filling
specific gaps (cost tracking, tamper-evident evidence, workspace fingerprinting, command
leashing) — not in adopting anyone's architecture wholesale.

### How this was compiled

Five parallel read-only research agents deep-read 9 repos (README + docs + 3–8 key source
files each, via `gh api` trees and `raw.githubusercontent.com`; no clones, no writes). Findings
are source-confirmed but **not a full code audit** — re-read the cited files before lifting any
code or schema, and re-check licenses (they change). Landscape context and star counts were
taken from GitHub metadata on the snapshot date.

## TL;DR

The **parallel-worktree-runner** category (spawn N agents in worktrees + dashboard/TUI) is a
saturated commodity — dozens of tools. Do **not** build there. The **verify-before-accept
control-plane** category is a much smaller niche (~8 live peers) and is where ACP lives; none
of them replicate ACP's full combination (durable DAG + dependency-lock + human root-acceptance
+ checkpoint-that-never-merges + contract-hash-bound controller gates + `ide_mcp`).

What the ecosystem has that **we lack or are weaker on** (the real deltas):

1. **Cost / token / spend accounting** — we have per-runner telemetry (`claude_telemetry.py`)
   but no unified cost schema or pricing model. *(mission-control, gnap, martin-loop)*
2. **Shell-free destructive/secret/path command leash.** *(martin-loop `leash.ts`)*
3. **Workspace before/after fingerprint + mutation-seq staleness** — a concrete mechanism for
   our asserted "bind evidence to checkpoint tree / gate-mutation-fails-closed / late-edit
   quarantine." *(OMK)*
4. **Cross-slot file-conflict pre-check.** *(swarm-protocol, OMK lane-grant auditor)*
5. **Tamper-evident evidence chaining / signed lineage / signed receipts** (optional,
   high-assurance / air-gap track). *(bernstein, martin-loop, agentplane ACR, OMK)*
6. **Git-snapshot rollback boundary** (restore-on-reject, complements quarantine). *(martin-loop)*
7. **Delegated-verification / anti-self-attestation** formalization. *(Ivy-Tendril, agentplane, OMK `notEvidenced[]`)*
8. **Log/token compression** for `runs/<job-id>/` + bounded excerpts. *(h5i token filter)*
9. **Multi-candidate compete-and-verify** with a neutral verdict policy (big, optional). *(h5i, bernstein)*
10. **Local read-only observability** over the SQLite stores (we are CLI-only). *(mission-control)*

## Prioritized porting backlog

Kind: `CODE` = source is a reusable license + language lets us copy/adapt directly;
`SCHEMA` = copy the data shape / SQL, reimplement logic; `IDEA` = concept only (foreign
language or NOASSERTION → clean-room reimplementation). Effort: S/M/L.

### Tier 1 — high value, good fit, do first

| # | Item | Source(s) | Kind | ACP target | Effort |
|---|------|-----------|------|-----------|--------|
| 1 | **Unified cost/token model**: `runs` cost columns (`cost_input/output/cache_read/cache_write_tokens`, `cost_usd`, `cost_model`, `provider`, `duration_ms`) + `token_usage` table + `MODEL_PRICING` map + `calculateTokenCost`; add martin-loop's `CostProvenance = actual\|estimated\|unavailable` label so estimates are never conflated with measured spend. Reconcile with existing `claude_telemetry.py`. | mission-control (SCHEMA+CODE, MIT); martin-loop (IDEA, Apache); gnap Run entity (SCHEMA, MIT) | SCHEMA+CODE | `entities/job/model/store.py`; `shared/` pricing module; `features/agent_runner/lib/claude_telemetry.py` | M |
| 2 | **Shell-free command leash**: destructive-command blocklist (`rm -rf`, `git reset --hard`, `curl\|sh`, `del/rmdir`, `find -delete`, …), path allow/deny + outside-repo guard, secret-pattern redaction, change-approval for dep/migration/config files. Pure regex/path evaluators, no shell — fits our "native gates run without a shell." | martin-loop `core/src/leash.ts` (CODE, Apache-2.0 → NOTICE) | CODE (TS→Py) | `shared/native_quality.py` + `features/agent_runner` pre-exec | M |
| 3 | **Workspace fingerprint + mutation-seq staleness**: `WorkspaceFingerprint` = git kind (`headCommit`, `changedPaths`, staged/unstaged diff SHA-256, dirty SHA-256) or artifact-set (per-path SHA-256 + manifest SHA-256); capture before/after each gate; a monotonic mutation seq auto-invalidates any prior evidence whose `seq <= latestMutationSeq`. Concretely implements "gate that mutates the worktree fails closed" + late-edit → quarantine. | OMK `guardrails/workspace-fingerprint.ts` + `evidence-system.ts` (IDEA, MIT) | IDEA (TS→Py) | `shared/native_quality.py`; `entities/workspace`; `features/plan_supervision` | M |
| 4 | **Cross-slot file-conflict pre-check**: set-intersection over declared `files_touching` across active claims → advisory warning before dispatch; optionally OMK's `PathConflict{severity: advisory\|merge-blocked}` static overlap auditor over per-slot allow/block path scopes. | swarm-protocol `findConflicts()` (SCHEMA, MIT); OMK `lane-grant-auditor` (IDEA, MIT) | SCHEMA/IDEA | `features/plan_supervision` (pre-dispatch); `entities/slot` | M |

### Tier 2 — strong, more optional or more effort

| # | Item | Source(s) | Kind | ACP target | Effort |
|---|------|-----------|------|-----------|--------|
| 5 | **Delegated-verification / no-self-attestation**: formalize inline (worker runs) vs delegated (a separate controller-owned process runs it; the worker *cannot* mark Pass) as an explicit field on gates. Add agentplane's CI gate booleans (`requirePlanApproved`/`requireVerification`/`requirePolicyPass`, `allowWaived`/`allowManualOverride`) and OMK's `notEvidenced[]` (enumerate what evidence does **not** cover) so acceptance can't be spoofed. | Ivy-Tendril (IDEA only, NOASSERTION); agentplane (IDEA, MIT); OMK (IDEA, MIT) | IDEA | `features/plan_supervision`; `verification.json` schema; `entities/review_inbox` | S/M |
| 6 | **Git-snapshot rollback boundary**: capture `git_head_plus_snapshot` (HEAD + dirty + untracked, base64) pre-attempt; on reject/drift, restore via `git restore`/checkout + delete new files; record `RollbackOutcome = restored\|not_required\|failed\|unavailable`. Complements checkpoint quarantine with an actual restore path. | martin-loop `core/src/rollback.ts` (CODE→IDEA, Apache) | CODE→IDEA | `features/slot_lifecycle` / `features/lifecycle_cleanup` | M |
| 7 | **Tamper-evident evidence chaining** (pick ONE assurance level; port the *contract* over SQLite, keep our store): (a) hash-chained transition ledger with redact-before-hash + divergence detection (`compare_chains`); (b) RFC-6962 Merkle daily seal of review-inbox/checkpoint evidence; (c) HMAC-signed run receipt with an explicit `EVIDENCE_BOUNDARY` verdict when budget/rollback/verifier evidence is missing; (d) digest-signed ACR projection. | bernstein `work_ledger.py`/`merkle.py`/`spine.py` (CODE, Apache→NOTICE); martin-loop receipt (IDEA); agentplane ACR (IDEA) | CODE/IDEA | `shared/verification_report.py`; `entities/review_inbox`; (optional new `entities/lineage`) | M/L |
| 8 | **Log/token compression filter**: line-scoring (panic 1.0 … noise 0.1) + `normalize_template()` numeric folding (`(×N)`) + per-command adapters (pytest/eslint summary); keep head/tail verbatim, store raw bytes out-of-band, only the summary travels. Rules as TOML assets with embedded golden tests. | h5i `token_filter.rs` + `assets/filters/*.toml` (CODE→IDEA, Apache) | CODE→IDEA (Rust→Py) | `features/agent_runner` / `features/result_handoff` bounded excerpts | M |
| 9 | **Richer evidence-record shapes**: OMK Evidence Receipt v3 (`command` argv/shell exact bytes + `workspaceBefore/After` + digest-only `output` + `disposition` + command-SHA-256 binding); agentplane evidence-kind vocab (`approval`, `context_manifest`, `changed_paths`, `check_result`, `quality_report`, `commit`, `artifact`, `external_link`); gnap Run entity (`attempt`, `tokens`, `cost_usd`, `commits[]`, `artifacts[]`, `result`/`error`). Enrich `verification.json` v2 / the review-inbox handoff payload. | OMK, agentplane, gnap (SCHEMA/IDEA, MIT) | SCHEMA/IDEA | `shared/verification_report.py`; `features/result_handoff`; `entities/review_inbox` | S/M |
| 10 | **Resume-vs-redo idempotency**: resume an interrupted attempt only if commits + verifications + reports + clean-worktree are all intact, else redo from clean; journaled gate resumes without re-asking; ledger `resume` replays the chain to rebuild scheduler state. | Ivy-Tendril (IDEA); h5i `gate.rs` (IDEA); bernstein `work_ledger.py` (IDEA) | IDEA | `features/agent_runner`; `features/slot_lifecycle` | M |
| 11 | **Preflight budget gate + provenance**: estimate projected spend BEFORE each attempt and stop if `projected > policy`; split settlement into patch vs verification cost; exit reasons `budget_exit\|diminishing_returns\|stuck_exit\|human_escalation`. Harden the existing quota broker. | martin-loop `contracts/index.ts` (IDEA, Apache) | IDEA | `features/agent_runner/lib/quota_broker.py`; `shared/config.py` | M |

### Tier 3 — big / strategic / possibly out of scope

| # | Item | Source(s) | Kind | ACP target | Effort |
|---|------|-----------|------|-----------|--------|
| 12 | **Multi-candidate compete-and-verify**: run N agents on the same task, cross-review, verify each in a fresh workspace, pick by a neutral `VerdictPolicy` (built-in rule: keep candidates whose latest verification applies cleanly + passes tests, refuse divergent verifier commands, pick the smallest diff; LLM judges are just pluggable policies). Changes our 1-job→1-checkpoint→human model — evaluate as opt-in. | h5i `judge.rs` (IDEA, Apache); bernstein tournament (IDEA) | IDEA | `features/plan_supervision` + `features/result_handoff` | L |
| 13 | **Local read-only observability** over the SQLite stores: kanban of task states, cost-tracker panel, activity/event stream, run-review, slot health. Thin web/TUI, no multi-tenant. | mission-control (IDEA blueprint, MIT) | IDEA | new thin read-only surface over `entities/*` stores | L |
| 14 | **Declarative table-driven phase-gate contract**: express the lifecycle as an ordered node table (`protected`, `evidence[]`, `allowedCommands`, `policyModules`, `cwd = base_checkout\|task_worktree`) instead of imperative code; add Ivy's pre-exec gate battery (dependency-satisfied, worktree-isolation, drift-vs-plan, dirty-repo) as fail-closed preconditions. | agentplane `workflow-lifecycle/contract.ts` (IDEA, MIT); Ivy-Tendril (IDEA) | IDEA | `features/plan_supervision`; `features/slot_lifecycle` | M/L |
| 15 | **Persistent cross-run memory** as a git ref (`refs/*/memory`) with `linked-commit: <code_oid>` correlating a memory snapshot to the code state it was learned from. Fits our checkpoint-ref discipline; scope carefully (could bloat). | h5i `memory.rs` (IDEA, Apache) | IDEA | `features/slot_lifecycle` / checkpoint | M/L |
| 16 | **`FrameworkAdapter` interface** (`register/heartbeat/reportTask/getAssignments/disconnect`, output-parse + cost per adapter) to formalize multi-runtime — only if we add runtimes beyond codex/claude. | mission-control (IDEA, MIT) | IDEA | `features/agent_runner` | M |

## Per-project reference cards

Jump here for source links when you pick a backlog item. Format: what it is · license · maturity · **take** / **skip** · key files.

### OMK — `dmae97/open-multi-agent-kit` · 127★ · TS · MIT
Provider-neutral wrapper around a vendored coding agent; gates completion behind
execution-bound evidence receipts. Real, active (v0.91). Filesystem-JSON state (no DB).
Its "DAG" is an *intra-turn tool-call* scheduler + per-subagent `LaneGrant`, **not** a durable
cross-task DAG — ACP's plans are more mature here.
- **Take:** Evidence Receipt v3 schema (#9), `WorkspaceFingerprint` + mutation-seq staleness (#3), lane-grant path-conflict auditor (#4), `notEvidenced[]` (#5).
- **Skip:** filesystem-JSON substrate; opt-in auto-commit; treat lanes as durable tasks.
- Files: `packages/coding-agent/src/types/evidence.ts`, `.../guardrails/workspace-fingerprint.ts`, `.../guardrails/evidence-system.ts`, `.../types/lane-grant.ts`, `docs/adr/evidence/omp-g3-verification.json`.

### bernstein — `chernistry/bernstein` · 711★ · Python · Apache-2.0
Deterministic, zero-LLM-in-the-loop orchestrator; hash-chained JSONL ledgers, RFC-6962 Merkle
seals, signed lineage spine (JCS + operator-HMAC + Ed25519), `EvidenceBundle` signing,
byte-identical *decision* replay, air-gap profile. Production-grade. **Same language as ACP** →
highest direct-code-reuse potential, but it's file-JSONL + **auto-merge by default**.
- **Take:** hash-chained ledger contract, Merkle seal, signed lineage, evidence bundle (#7); worktree symlink/hardlink leak checks (`worktree_isolation.py`, S — proves slot isolation we assert).
- **Skip:** auto-merge default (breaks delivery≠acceptance — **hard no**); file-JSONL storage (keep SQLite, port only the hashing *contract*).
- Files: `src/bernstein/core/persistence/work_ledger.py`, `.../persistence/merkle.py`, `.../lineage/spine.py`, `.../evidence/bundle.py`, `.../git/worktree_isolation.py`.
- Caveat: "byte-identical replay" is of the deterministic *scheduler decision path* over recorded outputs, **not** reproduction of model generations. Don't overclaim.

### AgentPlane — `basilisk-labs/agentplane` · 72★ · TS · MIT
Git-native evidence layer; phase-gated lifecycle (plan→approve→implement→verify→integrate);
digest-signed Agent Change Record (ACR). Mature, dogfooded (`.agentplane/tasks/*/acr.json`).
No auto-merge (integrate is a distinct orchestrator-driven step) — philosophically aligned with ACP.
- **Take:** ACR record shape with `result.merge_ready` separate from merge (#9), declarative phase-gate contract (#14), CI gate booleans + recorded approval (#5).
- **Skip:** git-markdown store as primary; CLI/recipes/context-wiki machinery.
- Files: `packages/agentplane/src/workflow-lifecycle/contract.ts`, `.../policy/rules/phase.ts`, `.agentplane/tasks/202605031625-886KZ6/acr.json`, `.../commands/acr/summary.ts`.

### martin-loop — `Keesan12/martin-loop` · 39★ · TS · Apache-2.0
Governs a single Ralph loop: budget caps, safety leash, git-snapshot rollback, HMAC run
receipts; KEEP/DISCARD/ESCALATE/HANDOFF per attempt. Compact; the loop-runtime itself sits
behind an OSS boundary (not in public `src`). **Auto-KEEP on verifier pass** — do not adopt.
- **Take:** `leash.ts` shell-free guard (#2, best single code-port target), preflight budget + provenance (#11), rollback boundary (#6), signed receipt + `EVIDENCE_BOUNDARY` verdict (#7).
- **Skip:** auto-KEEP acceptance (breaks delivery≠acceptance); JSONL as primary store.
- Files: `packages/core/src/leash.ts`, `packages/core/src/rollback.ts`, `packages/contracts/src/index.ts`, `docs/examples/proof-receipts/live-governed-run-receipt.json`.

### h5i — `h5i-dev/h5i` · 480★ · Rust · Apache-2.0
Git-native auditable sandboxes; multi-agent compete-and-verify with a neutral `VerdictPolicy`;
aggressive log/token compression; git-ref persistent memory; cgroup/seccomp sandbox tier.
Real, 4 crates, published `pip install h5i-orchestra` SDK.
- **Take:** compressed-log filter (#8), multi-candidate `VerdictPolicy` (#12), journaled fail-closed gate resume (#10), git-ref memory (#15).
- **Skip:** container/cgroup/seccomp sandbox layer (ACP is native/`ide_mcp`); the Rust SDK.
- Files: `crates/h5i-core/src/token_filter.rs`, `crates/h5i-orchestra/src/judge.rs`, `.../src/gate.rs`, `crates/h5i-core/src/memory.rs`, `assets/filters/eslint.toml`.

### mission-control — `builderz-labs/mission-control` · 5.8k★ · TS · MIT
Self-hosted local dashboard/control-plane; dispatch, review runs, track spend across many
runtimes; better-sqlite3 + WAL. Real but Alpha ("schemas may change"). Most popular here — but
it's a dashboard/ops layer; its acceptance gate (`quality_reviews`) is weaker than our
contract-hash-bound gates.
- **Take:** `runs` cost schema + `token_usage` + `MODEL_PRICING`/`calculateTokenCost` (#1, most directly portable), conditional-`UPDATE` atomic claim + deferred reconcile (harden dispatcher), `FrameworkAdapter` (#16), dashboard blueprint (#13).
- **Skip:** multi-tenant (`tenants/workspaces/api_keys`); Next.js/React stack; OpenClaw gateway provisioning.
- Files: `src/lib/schema.sql`, `src/lib/migrations.ts`, `src/lib/token-pricing.ts`, `src/lib/task-dispatch.ts`, `src/lib/adapters/adapter.ts`.

### swarm-protocol — `phuryn/swarm-protocol` · 49★ · TS · MIT
Headless MCP server: agents claim intents, declare files they'll touch, heartbeat, complete
with `unblocks[]`. Small but real. **Postgres/JSONB** store; conflicts are **advisory only** and
files are self-declared (worthless if a worker touches undeclared files).
- **Take:** `findConflicts()` set-intersection algorithm (#4), Claim/heartbeat/Signal shapes.
- **Skip:** MCP transport as the coordination layer; Postgres; automatic dependent-unblock (our root-acceptance gate is deliberately stronger).
- Files: `docs/SPEC.md`, `src/db/queries.ts`, `src/types.ts`.

### gnap — `farol-team/gnap` · 71★ · spec-only · MIT
Git-Native Agent Protocol — RFC draft, **never implemented**. Task board as JSON files under
`.gnap/`, coordinated by git pull/commit/push. "Dependencies" = `parent` + `blocked` flag, not
a DAG. Value is schema validation only.
- **Take:** Run entity schema (per-attempt `tokens`/`cost_usd`/`commits[]`/`artifacts[]`) (#1/#9); commit-convention audit trail.
- **Skip:** git-as-database substrate (contradicts SQLite-primary); optimistic git-push claiming.
- Files: `README.md` (normative spec), `examples/.gnap/runs/FA-1-1.json`.

### Ivy-Tendril — `Ivy-Interactive/Ivy-Tendril` · 165★ · C# · **NOASSERTION → IDEAS ONLY**
Agent-agnostic local orchestrator; plan-as-folder lifecycle; controller-owned verification gates
block promotion to human review. Real, substantial (.NET). **No license → never copy code or
text; express concepts independently.**
- **Take (ideas):** delegated-verification / no-self-certification (#5, strongest validation of ACP's direction), pre-exec fail-closed gate battery (#14), resume-vs-redo idempotency (#10), job-log externalization keyed `{jobId}-{planId}-{promptware}` so history survives a plan reset.
- **Skip:** everything code/text (license); PR-merge as the dependency signal (ACP is local, checkpoint-ref based).
- Files (concepts only): `.claude/skills/tendril-debug-plan/references/verification-pipeline.md`, `src/Ivy.Tendril.Docs/Docs/02_Concepts/{01_Plans,03_Lifecycle}.md`.

## Do NOT port (anti-patterns vs ACP invariants)

- **Auto-merge / auto-accept / auto-KEEP-on-verifier-pass** (bernstein default, martin-loop,
  h5i, OMK opt-in). Collapses **delivery ≠ acceptance**; the human root gate is the point. Hard no.
- **Git-as-database** (gnap; agentplane's markdown store) and **file-JSONL / Postgres substrates**
  (bernstein, OMK, martin-loop, swarm-protocol). Keep SQLite-primary; port only schemas/contracts on top.
- **Optimistic git-push claiming** (gnap). Our durable one-shot dispatcher is race-free on one machine.
- **Container/cgroup/seccomp sandbox tier** (h5i). ACP is `native` / `ide_mcp`; out of scope.
- **Multi-tenant** tenants/workspaces/api-keys (mission-control). Single-user local.
- **PR-merge as dependency signal** (Ivy). We gate on checkpoint refs + root acceptance, not `gh pr view`.
- Do not weaken **controller-owned, contract-hash-bound, shell-free** gates into worker-reported ones.

### Differentiators to protect (don't let a port erode these)

Delivery ≠ acceptance · fail-closed everywhere · human root-acceptance unlocks dependents ·
checkpoint records a ref + inbox item but **never** pushes/merges/moves the branch ·
evidence bound to an immutable contract hash + checkpoint tree · agents are untrusted, the
controller is the trust boundary · SQLite-primary durability · local/Windows-first, not a
cloud/k8s throughput factory · `native` vs `ide_mcp` workspace access with per-slot module isolation.

## License & attribution rules

- **MIT / Apache-2.0** (OMK, bernstein, agentplane, martin-loop, h5i, mission-control,
  swarm-protocol, gnap): code and ideas may be ported. If you copy/adapt actual **source or a
  distinctive schema/algorithm/data table**, retain attribution; for **Apache-2.0** also carry the
  license header and add a `NOTICE`/`THIRD-PARTY` entry (and note the patent grant). A clean-room
  reimplementation of a documented *idea* does not strictly require attribution, but record the
  source here anyway for provenance.
- **Ivy-Tendril = NOASSERTION** → treat as all-rights-reserved. **Ideas only. Never copy code or
  documentation text**; express concepts independently and cite as inspiration, not source.
- Most sources are TS/Rust/C# → most ports are reimplementations in Python regardless; the
  license still governs schemas/algorithms/text you lift verbatim.
- When the first port lands, create `docs/NOTICES.md` (or `THIRD-PARTY-NOTICES`) and list each
  borrowed source there.

| Repo | License | Code-port? | Notes |
|------|---------|-----------|-------|
| dmae97/open-multi-agent-kit | MIT | yes (TS→Py) | retain MIT notice if copying source |
| chernistry/bernstein | Apache-2.0 | yes (Python, direct) | NOTICE + license header + patent grant |
| basilisk-labs/agentplane | MIT | yes (TS→Py) | — |
| Keesan12/martin-loop | Apache-2.0 | yes (TS→Py) | NOTICE; `leash.ts` is the prime code-port |
| h5i-dev/h5i | Apache-2.0 | yes (Rust→Py) | NOTICE |
| builderz-labs/mission-control | MIT | yes (TS→Py) | schema/SQL + pricing table are the ports |
| phuryn/swarm-protocol | MIT | yes (TS→Py) | — |
| farol-team/gnap | MIT | schema only | spec, never implemented |
| Ivy-Interactive/Ivy-Tendril | **NOASSERTION** | **NO** | ideas only, no code/text |

## Recommended first three spikes

Highest leverage, lowest risk, best fit — start here:

1. **#1 Unified cost/token model** (mission-control schema + pricing, reconciled with
   `claude_telemetry.py`). Fills a real gap, additive, universally useful, no invariant risk.
2. **#2 Shell-free command leash** (martin-loop `leash.ts` → Python). Strengthens the
   untrusted-worker stance, near-verbatim port into the existing native-gate path.
3. **#3 Workspace fingerprint + mutation-seq staleness** (OMK). Turns three properties we
   currently *assert* (bind-to-checkpoint-tree, gate-mutation-fails-closed, late-edit-quarantine)
   into a concrete, testable mechanism.

Each is self-contained, lands in an existing slice, and does not touch the acceptance model.
