# Implementation Roadmap

Status: Phase 0, revision 2 (after Codex review round 1) · 2026-10-22

Every ATLAS phase follows **PLAN → CHALLENGE (Codex) → IMPLEMENT → TEST → REVIEW → FIX → VERIFY → COMMIT → PUSH → PR → MERGE → CHECKPOINT**. Until ATLAS can run itself (after Phase 5), this loop is driven by hand: Claude Code implements, `codex exec -s read-only` reviews, and the human approves the merge.

**Fail-closed rule (ATLAS-017):** every release must satisfy its own safety invariants with default config. A capability that isn't built yet is **absent and disabled**, never stubbed to "pass". For example, until the test sandbox exists, G-TEST cannot pass, so no phase can reach delivery. Each phase is split into PR-sized slices (under ~600 changed lines excluding lockfiles and fixtures). Every slice leaves `main` green.

---

## Phase 1 — Core foundation
**Objective:** a working local app with persistence, projects, tasks, config, FSM, repository locks, safe restart and a basic dashboard. No agents yet.

| Slice | Content | Tests |
|---|---|---|
| 1.1 | Scaffold: `backend/` (uv, FastAPI, ruff, mypy, pytest), `frontend/` (Vite React TS Tailwind, Vitest), GitHub Actions CI (lint, type, test, pip-audit, npm audit), `atlas serve` | CI green; `/healthz` |
| 1.2 | DB: models + Alembic baseline (projects with unique repo, tasks, plans, phases, candidates, transitions, audit_log, approvals, repo_locks); composite ownership FKs; WAL pragmas; single writer; backups | Migrations; pragma asserts; append-only triggers; **cross-project FK negative tests** |
| 1.3 | Config: Pydantic schemas, layering, safe defaults, unsafe values rejected, `policy_version` bump on change | Merge property tests; unsafe values rejected |
| 1.4 | FSM + policy engine: transition tables, `apply_transition` txn, guards as pure functions bound to candidate IDs; repo lock acquire/release | Invalid-transition table; Hypothesis stateful (no MERGED without same-candidate evidence); concurrent writers; **repo lock exclusivity** |
| 1.5 | API: projects (register record + `gh repo view` check, no clone yet), tasks CRUD, pause/resume/cancel, SSE; localhost bind, Host allowlist, session token, CSRF | API tests incl. CSRF/Host negatives |
| 1.6 | **Minimal safe restart:** instance lock; on startup every non-terminal task/phase → PAUSED, no auto-resume | Restart test: state preserved, nothing resumes by itself |
| 1.7 | Dashboard v1: Overview, Project, Task (state + timeline from `transitions` only) | Vitest; Playwright smoke |

**Acceptance:** register a project, create a task, and pause, resume or cancel it. The timeline shows only persisted transitions. After a restart, the state is identical and nothing proceeds without the user.
**Risk:** low.

## Phase 2 — Agent integration
- 2.0 **Spike, timeboxed, results in `docs/evidence/`:** resolve every "to verify" item in agent-compatibility.md and security-model.md §5.2:
  - Claude: `--setting-sources user` vs project CLAUDE.md/skills; usage-limit result shape; auth probe; OS sandbox via settings.
  - Codex: `--ignore-user-config` + `-c` pins and the effective config; read scope of sandboxed commands; `workspace-write` network; limit/auth failure signatures.
  - Use tiny prompts to save quota.
- 2.1 `atlas-exec` launcher + supervisor: durable launch handshake, run files, process groups, quiesce protocol, timeouts, artefacts, redaction, env scrub.
- 2.2 Blob store + `artifact_refs` + `executions`.
- 2.3 `AgentAdapter` protocol, `FakeAdapter` fixture replay, capability probe (version + help hash + required flags).
- 2.4 `ClaudeAdapter`, `CodexAdapter`, and `ManualHandoffAdapter` (packet + import validation, criteria completeness).
- 2.5 Capacity manager + Agent Status screen.

**Tests:**
- fixtures for success, malformed JSON, timeout, crash, rate_limit, auth expired, model_not_entitled and missing capability;
- hostile repo (project hooks/.mcp.json don't run);
- hostile user config (Codex MCP server ignored);
- env-scrub;
- crash between launch and run-file.

**Acceptance:** real auth and capacity are shown for Claude and Codex, and `MANUAL_ONLY` for Antigravity. A read-only "hello" execution per headless agent is recorded.

## Phase 3 — Planning & challenge
- Repository analysis (deterministic scan, then a read-only Claude summary), plan schema with criterion IDs, DAG validation, plan revisions.
- Codex challenge loop with planner responses, capped cycles, escalation to the user, and plan approval UI.

**Acceptance:** on a fixture repo, a request yields an approved, challenged, versioned plan with every finding answered.

## Phase 4 — Implementation workflow (local only, no GitHub)
- Worktree manager; developer execution; **candidate commits** and staleness; diff guards; snapshot-before-reset.
- **Test sandbox** (Seatbelt profile + canary self-test, or the dedicated-user option per D11). G-TEST is impossible without it.
- Code review on the candidate diff; repair loop; fix verification by an independent reviewer.
- **Minimum QA gate:** Antigravity handoff for the candidate with a dedicated QA worktree, import, criteria completeness and verdict consistency, plus optional approved reassignment. Without a valid QA result or waiver, the phase cannot reach READY_FOR_DELIVERY.

**Acceptance:** on a local fixture repo, one phase reaches READY_FOR_DELIVERY with these, all bound to one candidate:
- tests `executed` inside the sandbox;
- an independent review;
- an imported QA result.

A new fix makes the earlier evidence stale. A failing test triggers a bounded repair loop and then BLOCKED. The sandbox self-test shows that `~/.atlas` and `~/.ssh` are unreadable from tests.

## Phase 5 — GitHub delivery
- Outbox; push of the candidate; single PR per phase with update path; CI polling bound to the head SHA; protection discovery (known/unknown/user-declared); G-MERGE-PRE re-run before execution; merge with `--match-head-commit`; G-MERGED head verification; `MERGE_PENDING_REMOTE` handling; base update; cleanup.
- `merge_queue_support` stays **off**: merge-queue and unknown-protection repos get "user merges, ATLAS verifies".
- Outbox reconciliation for every op it introduces ships in this phase, not later.

**Acceptance:** the fake-gh suite passes, covering duplicate-PR prevention, interrupted merge, external head push, approval staleness and unknown protection. A live smoke test runs on a throwaway private sandbox repo (user-approved). Auto-merge remains off by default.

## Phase 6 — Capacity & full recovery
- Full startup reconciliation (candidates, executions, outbox, GitHub), `recovery.auto_resume` option, retry scheduling, provider session resume.

**Acceptance:** every row of the crash-point matrix (recovery-strategy.md §5) passes in CI.

## Phase 7 — QA enhancements
- `atlas handoff import` CLI; Playwright-based UI checks executed **by ATLAS** in the sandbox (`executed` evidence); a QA dashboard; review-resolution tracking; reassignment UX.

## Phase 8 — Multi-project scheduling
- Priority queue across projects, fairness (aging) and per-provider concurrency limits. Repo locks already exist from Phase 1.

**Acceptance:** with 3 fixture projects, higher-priority work runs first, starvation is bounded, and there is never more than one code-changing phase per repository.

## Phase 9 — Dashboard completion
- Execution timeline, diff viewer, findings dashboards, approvals inbox, recovery controls, GitHub status, audit history, redacted diagnostics export, notifications, final task report.

## Phase 10 — Hardening
- Security test suite (security-model.md §6), provider/GitHub failure simulation, install script, `launchd` agent, backup/restore docs, optional dedicated-user setup.
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

**Known limitation to report at DoD:** step 11 cannot be *fully autonomous* with Antigravity, because of its terms. It completes through a user-run manual handoff, or through approved reassignment to the non-implementing headless agent.
