# ADR-002: SQLite WAL + transactional outbox instead of a workflow engine

- Status: Proposed
- Date: 2026-10-22

## Context
ATLAS must survive crashes and restarts and must never duplicate external side effects. It runs on one Mac for one user, at low volume.

## Decision
- Persist all workflow state in SQLite (WAL, `synchronous=FULL`) through a single writer.
- Model every external side effect as an `outbox_ops` row (intent → execute → confirm) with an idempotency key.
- On restart, reconcile against ground truth (git, `gh`) before retrying.
- The FSM records only confirmed facts.

## Alternatives
- **Temporal, Prefect, Hatchet:** these need extra servers.
- **DBOS:** Postgres-first in Python.
- **Pure event sourcing:** more machinery than needed. The append-only `transitions` table already gives an audit history.

## Consequences
- One file to back up, and nothing extra to operate.
- Reconciliation logic is custom per operation. That is a small, enumerable set, and each operation has a fault-injection test.
- Horizontal scaling is not possible. That's acceptable; it's a non-goal.
