# ATLAS — Autonomous Multi-Agent Software Engineering Orchestrator

ATLAS is a self-hosted, local-first orchestrator for **macOS and Linux**. It coordinates the AI coding agents *you* configure, and delivers software changes in small, independently reviewed, independently mergeable phases. Supported agents:
- subscription CLIs such as Claude Code and OpenAI Codex;
- open-weight models served locally;
- API-key agents you opt into;
- manual handoffs such as Google Antigravity.

Each person runs their own copy with their own accounts ([ADR-007](docs/adr/ADR-007-portable-pluggable-providers.md)).

> **Status: Phase 0 — feasibility and architecture only.** There is no runnable code yet.
> Start with [`docs/architecture.md`](docs/architecture.md) and [`docs/risks-and-decisions.md`](docs/risks-and-decisions.md).

## Phase 0 documents

| Document | Purpose |
|---|---|
| [how-to-use.md](docs/how-to-use.md) | **How to use ATLAS** (planned usage, tagged by delivery phase) |
| [architecture.md](docs/architecture.md) | Components, process model, trust boundaries |
| [agent-compatibility.md](docs/agent-compatibility.md) | Verified per-provider capability matrix and command templates |
| [antigravity-manual-handoff.md](docs/antigravity-manual-handoff.md) | Runbook for the manual Antigravity step |
| [security-model.md](docs/security-model.md) | Threat model and sandbox boundaries |
| [workflow-state-machine.md](docs/workflow-state-machine.md) | Task and phase state machines |
| [database-design.md](docs/database-design.md) | SQLite schema |
| [recovery-strategy.md](docs/recovery-strategy.md) | Checkpoints, outbox, crash reconciliation |
| [github-integration.md](docs/github-integration.md) | Branch / PR / CI / merge design |
| [implementation-roadmap.md](docs/implementation-roadmap.md) | Phases 1–10 |
| [risks-and-decisions.md](docs/risks-and-decisions.md) | Risks, mitigations, decisions needing approval |
| [adr/](docs/adr/) | Architecture decision records. ATLAS is built on **LangGraph** ([ADR-001](docs/adr/ADR-001-build-vs-adopt.md), [ADR-006](docs/adr/ADR-006-langgraph-engine.md)); agents and platforms are user-configurable ([ADR-007](docs/adr/ADR-007-portable-pluggable-providers.md)) |
| [evidence/](docs/evidence/) | Captured CLI `--help` and version output |
| [review/](docs/review/) | Independent Codex review of Phase 0 and responses |
