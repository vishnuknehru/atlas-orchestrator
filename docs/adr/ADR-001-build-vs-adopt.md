# ADR-001: Build ATLAS on LangGraph; do not adopt an orchestrator app

- Status: **Accepted by user on 2026-10-22** (revised; it supersedes the earlier "bespoke core" proposal). Pending Codex challenge of the revision.
- Date: 2026-10-22

## Context
The user asked to adopt an existing orchestrator **if it is secured, well tested in the market, and fast-tracks delivery**. Candidates were evaluated on 2026-10-22 using GitHub data (`gh api`), READMEs, published security advisories and official docs.

## Evaluation

| Candidate | Fit | Maturity | Security evidence | Verdict |
|---|---|---|---|---|
| Vibe Kanban (Apache-2.0, 28k★) | Worktrees, PR, merge | README: **"Vibe Kanban is sunsetting"**; last release 2026-04-24 | No advisories | ❌ Sunsetting |
| claude-squad (AGPL-3.0, 8.6k★) | tmux + worktrees only | Last release 2026-08-20, 20 contributors | No SECURITY.md | ❌ Weak fit |
| Orchestrator.inc agent-orchestrator (Apache-2.0, 13k★) | **Best functional fit** (planner, workers, reviewer agents, PR/CI kanban) | ~576 commits in 30 days, nightly releases, 988 open issues, project 8 months old | No SECURITY.md. README tells macOS users to bypass Gatekeeper for an unverified app. Docs: reviewer modes "do not sandbox tools or network access". Auto-update | ❌ Fails "secured" |
| Emdash (Apache-2.0, 6k★) | Worktrees, PR, CI, merge; no planning/review roles | ~222 commits in 30 days | No SECURITY.md. Writes hooks into user-level agent config. Optional telemetry | ❌ Fails "secured"; partial fit |
| OpenHands (MIT, 91k★) | Agent server driving Claude Code/Codex via ACP; no worktree/PR/review workflow | Very mature | Working disclosure process (high-severity command injection fixed 2026-03). Claude ACP adapter is built on the Agent SDK, which is a grey area for subscription use | ⚠️ Low fit |
| **LangGraph** (MIT, 43k★, since 2023, LangChain Inc.) | **Workflow engine**: state graphs, durable checkpoints, retries, `interrupt()` human-in-the-loop. Nodes are plain Python, so they can run `claude -p` / `codex exec` subprocesses on subscriptions with no API key | Releases every few days (1.2.16 on 2026-10-16), large user base | **Active disclosure process: 13 published advisories, including ZDI reports, all fixed.** Includes HIGH issues in the SQLite checkpointer (CVE-2025-67644, CVE-2025-64104 SQLi) and checkpoint deserialization RCE (CVE-2025-64439, CVE-2026-28277, CVE-2026-48775) | ✅ **Selected** as the engine |
| Temporal / Prefect / Hatchet / DBOS | Durable workflows | Mature | — | ❌ Need servers or Postgres |

## Decision
- **Adopt LangGraph** (`langgraph` + `langgraph-checkpoint-sqlite` only) as ATLAS's workflow engine. It provides the state graph, durable checkpointing, retry policies, and the interrupts that pause for approvals and manual handoffs. See [ADR-006](ADR-006-langgraph-engine.md) for how it is used safely.
- **Keep ATLAS-owned:**
  - the policy and gate engine;
  - the candidate/evidence model;
  - the outbox and GitHub reconciliation;
  - worktrees;
  - the test sandbox;
  - agent subprocess adapters;
  - the dashboard.

  LangGraph provides none of these, and they hold the safety guarantees challenged in Phase 0.
- **Do not adopt** an orchestrator *application* now. Re-evaluate Orchestrator.inc in 6 months: it would need a security policy, signed and notarized builds, and documented permission defaults.
- **Do not use** LangGraph Platform/Server, LangGraph Studio, LangSmith tracing, or any `langchain-*` model integrations. ATLAS is local-only, with no data egress and no API keys.

## Consequences
- **Expected faster, not yet proven (ATLAS-L07):** graph execution, checkpoint persistence, retries and interrupts come from a widely deployed, security-researched library. ATLAS still has to build the step-completion contract, pause protocol, two-store classification and a strict serializer (ADR-006), which offsets part of the gain.

  Compared with the bespoke design: LangGraph removes the custom graph runner and checkpoint persistence, but adds about four integration mechanisms. The gain is therefore a **hypothesis tested by roadmap slice 1.0**, a ≤ 3-day spike with pass/fail criteria. If it fails, ATLAS reverts to the bespoke engine through the `WorkflowEngine` interface, with no loss to the rest of the design.
- **New risks:** LangGraph replay semantics (a node re-runs from its start on resume), dependency churn, and the SQLite checkpointer's CVE history. These are mitigated in ADR-006 and security-model.md §3.10.
- **Unchanged:** the Phase 0 gates, evidence model and recovery invariants.

## Supply-chain controls
- Locked dependencies with hashes (`uv lock`), and versions pinned at or above the patched releases for every advisory above.
- `pip-audit` in CI, so any new GHSA blocks the build. Renovate PRs for upgrades, which go through the same gates.
- Licence check: MIT/BSD/Apache only.
