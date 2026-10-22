# ADR-001: Build a small bespoke orchestration core on audited libraries

- Status: Proposed (pending Codex challenge and user approval)
- Date: 2026-10-22

## Context
The user asked whether ATLAS should adopt an existing open-source orchestrator instead of building one. They added that the result "must be secured well". ATLAS's distinguishing requirements are:
- gated adversarial review with an evidence model;
- independence enforcement;
- idempotent, reconciled GitHub side effects;
- durable crash recovery;
- subscription-only, terms-respecting agent use (including manual Antigravity);
- strict prompt-injection boundaries.

## Options evaluated (desk research, 2026-10-22)

| Option | What it is | Fit | Security / trust concerns |
|---|---|---|---|
| Vibe Kanban, claude-squad, Conductor, Emdash, Rove | UIs/TUIs to run several coding agents side by side in worktrees | Covers worktree management only. No plan challenge, evidence-gated merges, outbox, or policy engine | Fast-moving; no published security audits found. Usually runs agents in permissive modes. Would hold gh credentials for every repo |
| Orchestrator.inc `agent-orchestrator` (Apache-2.0) | Desktop + mobile app for agent teams, task to merged PR | Closest in scope | Large surface (desktop + mobile + remote). Unclear credential handling and remote components. Adopting it means trusting the vendor's code path to merge into main |
| claw-orchestrator | Headless runtime for multiple CLIs incl. Antigravity; "done only when checks it didn't write pass" | Good ideas (independent checks) | Automates Antigravity, which conflicts with ADR-003. Spend-cap features are aimed at API billing |
| Temporal / Prefect / Hatchet | Durable workflow engines | Durable execution | Need a server and Postgres. Violates the "no unnecessary infra" constraint |
| DBOS Transact (MIT) | Library-level durable workflows | Attractive model | Python version is Postgres-first. SQLite support for Python not confirmed. Adds a framework whose semantics we must trust for recovery |
| LangGraph (+ SQLite checkpointer) | Agent graph framework | Checkpointing | Designed around direct LLM API calls, not CLI subprocesses. Heavy dependency for little gain |

Sources: [augmentcode survey](https://www.augmentcode.com/tools/open-source-agent-orchestrators), [OrchestratorInc/agent-orchestrator](https://github.com/OrchestratorInc/agent-orchestrator), [claw-orchestrator](https://github.com/Enderfga/claw-orchestrator), [DBOS Python](https://github.com/DBOS-project/dbos-transact-py), [DBOS June 2026 update](https://dbos.dev/blog/new-in-dbos-june-2026).

## Decision
**Build** a small core for the security-critical parts:
- FSM + transition log;
- policy engine (gates, independence, diff guards);
- outbox + reconciliation;
- supervisor;
- adapters;
- handoff validation.

Estimated size: roughly 3–5k lines of Python, each piece unit-testable in isolation.

**Adopt** well-known, widely deployed libraries and tools for everything else:
- FastAPI, Pydantic, SQLAlchemy/Alembic, SQLite, structlog;
- React, Vite, Tailwind;
- pytest, Hypothesis, Vitest, Playwright;
- the official provider CLIs, git, gh.

## Consequences
- **Plus:** full control over the parts where bugs lead to bad merges or terms violations. A minimal dependency surface, and no third-party code holding credentials.
- **Minus:** we write and maintain the recovery logic ourselves. This is mitigated by the crash-point fault-injection suite (recovery-strategy.md §5).
- **Reopen if:** a mature library emerges with SQLite-native durable workflows for Python, or the user prefers a specific tool after their own review.

## Supply-chain controls
- Locked dependencies with hashes.
- `pip-audit` / `npm audit` in CI.
- Renovate/Dependabot.
- Licence check: MIT/BSD/Apache only.
- Each new runtime dependency is justified in its PR.
