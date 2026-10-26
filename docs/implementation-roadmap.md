# Implementation Roadmap

Status: updated 2026-10-26 after PR #19. The plan below is unchanged; each phase now carries a **Build status** block, and the section "Where we are" lists what is done, what differs from the plan and what is next.

Every ATLAS phase follows **PLAN → CHALLENGE (Codex) → IMPLEMENT → TEST → REVIEW → FIX → VERIFY → COMMIT → PUSH → PR → MERGE → CHECKPOINT**. Until ATLAS can run itself (after Phase 5), this loop is driven by hand: Claude Code implements, `codex exec -s read-only` reviews, and the human approves the merge.

**Recovery travels with each effect (ATLAS-L04):** every phase that introduces a new kind of external effect also ships its recovery and reconciliation, plus a "restart → user Resume" test:
- Phase 2: processes (launch handshake, quiesce);
- Phase 4: candidates, snapshots and verification worktrees;
- Phase 5: outbox, GitHub reconciliation and remote-merge withdrawal.

Phase 6 adds only auto-resume, capacity scheduling and the full fault-injection matrix.

**Fail-closed rule (ATLAS-017):** every release must satisfy its own safety invariants with default config. A capability that isn't built yet is **absent and disabled**, never stubbed to "pass". For example, until the test sandbox exists, G-TEST cannot pass, so no phase can reach delivery. Each phase is split into PR-sized slices (under ~600 changed lines excluding lockfiles and fixtures). Every slice leaves `main` green.

## Where we are (2026-10-26)

A started task now runs: **clone and scan → architect plan → independent challenge (up to 3 rounds) → user approval → developer edits a worktree in the agent sandbox → candidate commit → tests in the sandbox → evidence bound to the candidate commit.** It then stops at `EXECUTING_PHASES`, because review, QA and delivery are not built (fail-closed: nothing can complete or be pushed).

| Phase | Status |
|---|---|
| 1 Core foundation | Done |
| 2 Agent integration | **Partial** (agent sandbox, adapter layer; no supervisor, blob store, manifests, manual handoff, capacity) |
| 3 Planning and challenge | Done, with gaps noted below |
| 4 Implementation workflow | **About half** (worktrees, candidate, sandbox, test runner done; review, repair, QA, dependency install pending) |
| 5 GitHub delivery | Not started |
| 6–10 | Not started |

### Deliberate differences from the plan
- **D16 (amends D11):** the sandbox uses the user's existing Colima VM. No dedicated `atlas` VM, no reserved APFS volume, no loop filesystems. Shared host folders are a warning, not a blocker. See risks-and-decisions.md.
- **Codex cannot edit files** inside the agent sandbox (its own sandbox will not nest; `docs/evidence/agent-boundary-spike-2026-10-26.md`), so **Claude Code is the default developer** and Codex the default reviewer. Defaults: architect claude, challenger codex, developer claude, reviewer codex.
- **Files instead of tables:** analysis, plan (with challenge history), candidate and test evidence are JSON files under `~/.atlas/projects/<id>/` (`analysis/`, `plans/`, `candidates/`, `tests/`). The `plans`, `candidates`, `executions`, `artifact_refs` and `approvals` tables exist but are not used yet; they need the blob store (2.2).
- **One candidate per task, not per phase.** The plan's steps are not materialised as `phases` rows yet; the whole plan is implemented as one candidate. Moving to per-step phases is a design decision to take before Phase 5.
- Steps run in a background task after Start/Retry/Approve, and the dashboard polls; there is no SSE client yet.

### Pending, in the suggested order
1. **Independent code review of the candidate diff** (reviewer must pass the independence check against the developer), findings the developer must answer.
2. **Bounded repair loop:** failing tests or blocking findings send the candidate back to the developer, then BLOCKED after the cap; any new commit makes tests and review stale.
3. **QA gate:** manual handoff packet for the configured QA agent (Antigravity), import validation, criteria completeness, then `READY_FOR_DELIVERY` (task states and a first delivery hold; nothing is pushed).
4. **Dependency install** for tests: egress proxy on an internal network, setup canary, per-project registry allow-list. Until then tests have no network, so the test image must already contain the dependencies.
5. **Decide the phases model** (one candidate per plan step) and the task/phase DB rows, then Phase 5 (outbox, push, PR, CI, merge).
6. **Held candidates:** an approval flow for guard violations (today they are only flagged).
7. Phase 2 leftovers: supervisor with run files and crash handling, blob store and `executions`, manifest-driven adapters and capability probe, manual-handoff and OpenAI-compatible adapters, capacity manager and Agent Status screen, API-key injection (D15), Linux boundary and sandbox checks.
8. Sandbox leftovers: safe `/out` report collection, per-run attestation of the Docker daemon, macOS-toolchain and source-mutation BLOCKED cases.


---

## Phase 1 — Core foundation
**Build status: done** (merged). Not re-audited: the Playwright smoke test of slice 1.7.
**Objective:** a working local app with persistence, projects, tasks, config, FSM, repository locks, safe restart and a basic dashboard. No agents yet.

| Slice | Content | Tests |
|---|---|---|
| **1.0** | **LangGraph integration spike, ≤ 3 working days, throwaway branch** (ADR-006 §8). Proves on pinned versions: §3 crash-window tests, §4 pause protocol, §5 thread classification and rebuild, §6 `StrictJsonSerializer` round-trip + hostile-blob rejection, zero egress. Records the actual effort | **Pass → continue with LangGraph. Fail → revert to the bespoke engine** behind the same `WorkflowEngine` interface. The result is recorded as an ADR-006 amendment and shown to the user |
| 1.1 | Scaffold: `backend/` (uv, FastAPI, ruff, mypy, pytest), `frontend/` (Vite React TS Tailwind, Vitest), GitHub Actions CI on **macOS and Linux** runners (lint, type, test, pip-audit, npm audit), `atlas serve` | CI green on both OSes; `/healthz` |
| 1.2 | DB: models + Alembic baseline (projects with unique repo, tasks, plans, phases, candidates, transitions, audit_log, approvals, repo_locks); composite ownership FKs; WAL pragmas; single writer; backups | Migrations; pragma asserts; append-only triggers; **cross-project FK negative tests** |
| 1.3 | Config: Pydantic schemas, layering, safe defaults, unsafe values rejected, `policy_version` bump on change. Includes the **agent registry** (`agents.yaml`, ADR-007 §2): roles reference registry ids; `billing`, `model_family`, loopback-only `endpoint`; `SecretStore` interface (Keychain / Secret Service) | Merge property tests; unsafe values rejected; role → registry resolution; ADR-005 independence over example profiles A and B; non-loopback endpoint rejected without opt-in |
| 1.4 | FSM + policy engine: transition tables, `apply_transition` txn, guards as pure functions bound to candidate IDs; repo lock acquire/release | Invalid-transition table; Hypothesis stateful (no MERGED without same-candidate evidence); concurrent writers; **repo lock exclusivity** |
| 1.4b | **LangGraph engine (ADR-006):**<br>• pinned patched versions, `WorkflowEngine` interface, `StrictJsonSerializer`, `durability="sync"`, tracing forced off<br>• `workflow_steps` step-completion contract<br>• pause fence protocol<br>• **startup thread classification + audited rebuild** (ships here, not in Phase 6)<br>• quiesced paired backups<br><br>Production graphs contain only **real** nodes for capabilities that exist. Nodes for unbuilt capabilities (agents, tests, GitHub) are **fail-closed placeholders**: they raise `CapabilityUnavailable` → `BLOCKED(capability_unavailable)`. **No-op nodes exist only in the test suite** | Graph edges ⊆ `ALLOWED`; crash-window tests (ADR-006 §3.5); pause tests; classification tests (consistent / app-ahead / paused overlay / incompatible); rebuild; restart → user Resume; zero egress; `pip-audit` clean |
| 1.5 | API: projects (register record + `gh repo view` check, no clone yet), tasks CRUD, pause/resume/cancel, SSE; localhost bind, Host allowlist, session token, CSRF | API tests incl. CSRF/Host negatives |
| 1.6 | **Minimal safe restart:** instance lock; on startup every non-terminal task/phase → PAUSED, no auto-resume | Restart test: state preserved, nothing resumes by itself |
| 1.7 | Dashboard v1: Overview, Project, Task (state + timeline from `transitions` only) | Vitest; Playwright smoke |

**Acceptance:** register a project, create a task, and pause, resume or cancel it. The timeline shows only persisted transitions. After a restart, the state is identical and nothing proceeds without the user.
**Risk:** low.

## Phase 2 — Agent integration
**Build status: partial.**
- Done: macOS Seatbelt `AgentBoundary` (reads of `~/.atlas`, credentials and Docker sockets denied; writes only in the worktree and scratch) with spike evidence; `AgentAdapter` protocol, `FakeAdapter`, exact-value redaction and an env allowlist; one `CliAdapter` for Claude Code and Codex; typed failures (rate limit, auth, timeout, not installed, no boundary); writer requests fail closed without a boundary.
- Not done: 2.0 spike items other than the boundary; 2.0b (Linux, local models, `credential_isolation`); 2.1 launcher/supervisor (handshake, run files, process groups, quiesce); 2.2 blob store, `artifact_refs`, `executions`; 2.3 manifests and capability probe; 2.4 `ManualHandoffAdapter` and `OpenAICompatibleHttpAdapter`; 2.5 capacity manager and Agent Status screen. The Phase 2 acceptance criteria are therefore not met.
- 2.0 **Spike, timeboxed, results in `docs/evidence/`:** resolve every "to verify" item in agent-compatibility.md and security-model.md §5.2:
  - Claude: `--setting-sources user` vs project CLAUDE.md/skills; usage-limit result shape; auth probe; OS sandbox via settings.
  - Codex: `--ignore-user-config` + `-c` pins and the effective config; read scope of sandboxed commands; `workspace-write` network; limit/auth failure signatures.
  - Use tiny prompts to save quota.
- 2.0b **Linux and local-model spike items** (ADR-007): bubblewrap/Landlock `AgentBoundary` with the same Docker-socket test; Codex `--oss` effective config and endpoint; one OpenAI-compatible server (Ollama) structured-output behaviour; `credential_isolation` for `claude_cli` and `codex_cli` (does an agent-launched `env` show an injected key?).
- 2.1 `atlas-exec` launcher + supervisor: durable launch handshake, run files, process groups, quiesce protocol, timeouts, artefacts, redaction, env scrub (with the per-agent API-key exception, D15). Also the **outer `AgentBoundary` wrapper for agent processes** (Seatbelt on macOS, bubblewrap on Linux), which denies `~/.colima`, `~/.docker`, `~/.atlas` and Docker sockets. Spike test: an agent command can't reach the Docker socket; shell-capable roles are BLOCKED if the wrapper fails (security-model.md §5.1.9).
- 2.2 Blob store + `artifact_refs` + `executions`.
- 2.3 `AgentAdapter` protocol, **manifest-driven generic `cli` adapter** (ADR-007 §3; only verified manifests load), exact-value secret redaction, `FakeAdapter` fixture replay, capability probe (version + help hash + required flags).
- 2.4 Built-in manifests `claude_cli` and `codex_cli`, `ManualHandoffAdapter` (packet + import validation, criteria completeness), and `OpenAICompatibleHttpAdapter` for read-only roles (loopback check after DNS resolution, schema-validated reply, no tools).
- 2.5 Capacity manager + Agent Status screen.

**Tests:**
- fixtures for success, malformed JSON, timeout, crash, rate_limit, auth expired, model_not_entitled and missing capability;
- hostile repo (project hooks/.mcp.json don't run);
- hostile user config (Codex MCP server ignored);
- env-scrub;
- crash between launch and run-file.

**Acceptance:** real auth and capacity are shown for Claude and Codex, and `MANUAL_ONLY` for Antigravity. A read-only "hello" execution per headless agent is recorded, including one local-model review through `openai_compatible_http`. A user-added manifest is refused. An API-key CLI agent without `credential_isolation: verified` is `BLOCKED(api_key_not_isolated)`.

## Phase 3 — Planning & challenge
**Build status: done, with gaps.**
- Done: clone (token never in argv, hooks off) and deterministic scan; architect plan with acceptance-criterion coverage; independent challenger (ADR-005 check), architect must answer every blocking finding, 3-round cap with escalation; user approve / request changes; Retry for blocked tasks.
- Gaps: no read-only agent summary of the repository; no DAG validation or dependencies between plan steps; plans and approvals are files, not rows.
- Repository analysis (deterministic scan, then a read-only Claude summary), plan schema with criterion IDs, DAG validation, plan revisions.
- Codex challenge loop with planner responses, capped cycles, escalation to the user, and plan approval UI.

**Acceptance:** on a fixture repo, a request yields an approved, challenged, versioned plan with every finding answered.

## Phase 4 — Implementation workflow (local only, no GitHub)
**Build status: about half.**
- Done: worktree manager; developer step (Claude, inside the agent boundary) producing a candidate commit; minimal diff guard (sensitive paths flagged `held`); sandbox status checks, self-test and canary; export from git objects with unsupported-feature blocking; source volume populate and attestation before/after; contract-checked containers (verified with `inspect` before `start`); per-command timeouts, kill and cleanup; orphan reaping; test evidence bound to the candidate commit with a stale flag; project test settings and dashboard panels.
- Not done: code review and fix verification; repair loop; QA gate and `READY_FOR_DELIVERY`; dependency egress proxy and setup canary; safe `/out` extraction; held-candidate approvals (G-DIFF); snapshot-before-reset; per-run daemon/boot-id attestation; reserved disk and loop filesystems (dropped by D16); Linux self-test. The Phase 4 acceptance (one phase at `READY_FOR_DELIVERY` with tests, review and QA bound to one candidate) is not met.
- Worktree manager; developer execution; **candidate commits** and staleness; diff guards; snapshot-before-reset.
- **Test sandbox: Docker on Colima** (D11; security-model.md §5.1), covering:
  - a candidate tar built from git objects, mounted read-only;
  - declared writable paths and size limits;
  - the create → inspect → start contract check;
  - a per-execution canary and in-VM manifest attestation;
  - safe `/out` extraction;
  - daemon-side lifecycle/recovery;
  - the dependency egress proxy.

  The **reserved disk** for the Colima VM (one-time admin step by the user: an APFS volume on macOS, a partition or LVM volume on Linux) and per-execution fixed-size loop filesystems are also part of this slice. The sandbox self-test runs on both OSes.

  G-TEST is impossible without it. macOS-toolchain projects, source-mutating tests, submodules and LFS stay BLOCKED.
- Code review on the candidate diff; repair loop; fix verification by an independent reviewer.
- **Minimum QA gate (provider-neutral, ADR-007):** QA by the configured QA agent. With the recommended profile, that is an Antigravity manual handoff; any `manual_handoff` agent works the same way. It uses a dedicated QA worktree, import, criteria completeness and verdict consistency. No reassignment path (D4). Without a valid QA result or waiver, the phase cannot reach READY_FOR_DELIVERY.

**Acceptance:** on a local fixture repo, one phase reaches READY_FOR_DELIVERY with these, all bound to one candidate:
- tests `executed` inside the sandbox;
- an independent review;
- an imported QA result (from the configured QA agent).

A new fix makes the earlier evidence stale. A failing test triggers a bounded repair loop and then BLOCKED. The sandbox self-test shows that `~/.atlas` and `~/.ssh` are unreadable from tests.

## Phase 5 — GitHub delivery
**Build status: not started.** Gated on the Phase 4 acceptance; there is no push, PR, CI or merge code.
- Outbox; push of the candidate; single PR per phase with update path; CI polling bound to the head SHA; protection discovery (known/unknown/user-declared); G-MERGE-PRE re-run before execution; merge with `--match-head-commit`; G-MERGED head verification; `MERGE_PENDING_REMOTE` handling; base update; cleanup.
- `merge_queue_support` stays **off**: merge-queue and unknown-protection repos get "user merges, ATLAS verifies".
- Outbox reconciliation for every op it introduces ships in this phase, not later.

**Acceptance:** the fake-gh suite passes, covering duplicate-PR prevention, interrupted merge, external head push, approval staleness and unknown protection. A live smoke test runs on a throwaway private sandbox repo (user-approved). ATLAS auto-merge is not available in this release (D6).

## Phase 6 — Capacity & full recovery
- `recovery.auto_resume` option, capacity-aware retry scheduling, provider session resume, and the **complete** crash-point fault-injection matrix in CI.
- The basic reconciliation for each effect already shipped with the phase that introduced it (see "Recovery travels with each effect"). The thread classification and rebuild shipped in 1.4b.

**Acceptance:** every row of the crash-point matrix (recovery-strategy.md §5) passes in CI.

## Phase 7 — QA enhancements
- `atlas handoff import` CLI; Playwright-based UI checks executed **by ATLAS** in the sandbox (`executed` evidence); a QA dashboard; review-resolution tracking.

## Phase 8 — Multi-project scheduling
- Priority queue across projects, fairness (aging) and per-provider concurrency limits. Repo locks already exist from Phase 1.

**Acceptance:** with 3 fixture projects, higher-priority work runs first, starvation is bounded, and there is never more than one code-changing phase per repository.

## Phase 9 — Dashboard completion
- Execution timeline, diff viewer, findings dashboards, approvals inbox, recovery controls, GitHub status, audit history, redacted diagnostics export, notifications, final task report.

## Phase 10 — Hardening
- Security test suite (security-model.md §6), provider/GitHub failure simulation, install script for macOS and Linux, `launchd` agent / `systemd --user` unit, `atlas agents add|test|list` and `atlas secrets set`, backup/restore docs, optional dedicated-user setup.
- An end-to-end Definition-of-Done run on a sandbox repo, recorded in `docs/evidence/e2e-<date>.md`.

## Definition-of-Done tracking
| DoD step (brief §24) | Phase |
|---|---|
| 1–2 register + request | 1 |
| 3–6 analyse, plan, challenge, revise | 3 |
| 7–12 select phase, implement, test, review, QA, resolve | 4 |
| 13–18 commit, push, PR, CI, merge, confirm | 4 (candidate commit) + 5 |
| 19–20 checkpoint, next phase | 4–5 |
| 21–22 pause on unavailability, resume after restart | 1 (safe pause) → 6 (full resume) |
| 23 final report | 9 |

**Known limitation to report at DoD:** step 11 cannot be *fully autonomous* with Antigravity, because of its terms. It completes through a user-run manual handoff (D4: wait; no reassignment).
