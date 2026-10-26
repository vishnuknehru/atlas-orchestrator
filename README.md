# ATLAS — Autonomous Multi-Agent Software Engineering Orchestrator

ATLAS is a self-hosted, local-first orchestrator for **macOS and Linux**. It coordinates the AI coding agents *you* configure, and delivers software changes in small, independently reviewed, independently mergeable phases. Supported agents:
- subscription CLIs such as Claude Code and OpenAI Codex;
- open-weight models served locally;
- API-key agents you opt into;
- manual handoffs such as Google Antigravity.

Each person runs their own copy with their own accounts ([ADR-007](docs/adr/ADR-007-portable-pluggable-providers.md)).

> **Status: Phase 1 — Core Foundation implemented.**
> Run `uv sync --extra dev` and `uv run atlas serve` to start the local ATLAS server and dashboard on `127.0.0.1:8420`.
> Start with [`docs/how-to-use.md`](docs/how-to-use.md), [`docs/architecture.md`](docs/architecture.md), and [`docs/risks-and-decisions.md`](docs/risks-and-decisions.md).

## Start and stop ATLAS

**One-time setup** (from the repository root):

```bash
uv sync --extra dev                        # install the backend
cd frontend && npm ci && npm run build && cd ..   # build the dashboard (served by the backend)
```

**Start**

```bash
uv run atlas serve                         # http://127.0.0.1:8420, opens your browser
uv run atlas serve --no-browser            # do not open a browser
uv run atlas serve --port 9000             # use another port (or set ATLAS_PORT)
uv run atlas serve --data-dir ~/my-atlas   # use another data directory (or set ATLAS_HOME; default ~/.atlas)
```

The terminal prints a one-time bootstrap login URL. Open it if the browser does not open by itself.
To run it in the background: `nohup uv run atlas serve --no-browser > atlas.log 2>&1 &`.

**Stop**

- Foreground: press `Ctrl+C` in the terminal running `atlas serve`.
- Background: `pkill -f "atlas serve"` (sends a normal terminate signal).

ATLAS allows one running instance per data directory. After a stop or crash, in-flight work is paused on the next start so you can resume it from the dashboard.

**Develop the dashboard with hot reload:** run `uv run atlas serve --no-browser`, then `cd frontend && npm run dev`.

## Plan a project in phases

Open a project in the dashboard, expand **Plan project in phases**, describe the goal and select **Create plan**. The project's *architect* agent (set in Settings; Claude Code or Codex) writes an ordered plan, and every phase is added to the project as a task with its own acceptance criteria. Tasks are not started automatically: start them in order. Use **New task** for one-off work.

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
