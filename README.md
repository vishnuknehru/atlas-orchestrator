# ATLAS — Autonomous Multi-Agent Software Engineering Orchestrator

ATLAS is a local-first macOS application. It coordinates official AI coding-agent CLIs (Claude Code, OpenAI Codex) and a manual-handoff Google Antigravity step. Together they deliver software changes in small, independently reviewed, independently mergeable phases.

> **Status: Phase 0 — feasibility and architecture only.** There is no runnable code yet.
> Start with [`docs/architecture.md`](docs/architecture.md) and [`docs/risks-and-decisions.md`](docs/risks-and-decisions.md).

## Phase 0 documents

| Document | Purpose |
|---|---|
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
| [adr/](docs/adr/) | Architecture decision records |
| [evidence/](docs/evidence/) | Captured CLI `--help` and version output |
| [review/](docs/review/) | Independent Codex review of Phase 0 and responses |
