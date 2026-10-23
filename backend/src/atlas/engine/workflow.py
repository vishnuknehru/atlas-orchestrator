"""LangGraph WorkflowEngine implementation (ADR-006 §2–§6, Slice 1.0 & Slice 1.4b)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from atlas.db.connection import DatabaseManager
from atlas.engine.serde import StrictJsonSerializer
from atlas.fsm.states import (
    PhaseState,
    TaskState,
    is_phase_transition_allowed,
    is_task_transition_allowed,
)
from atlas.fsm.transitions import (
    EntityNotFoundError,
    GuardFailedError,
    InvalidTransitionError,
    apply_phase_transition,
    apply_task_transition,
)
from atlas.utils import ensure_secure_file, new_ulid, utc_now_iso


class CapabilityUnavailableError(RuntimeError):
    """Raised by fail-closed placeholder nodes for capabilities built in later phases (ATLAS-017)."""


class DispatchFencedError(RuntimeError):
    """Raised when an ordinary effect dispatch is rejected because a pause/cancel fence is active."""


class RebuildPreconditionError(RuntimeError):
    """Raised when rebuild_thread is called while preconditions in ADR-006 §5.3 are not met."""


@dataclass(frozen=True)
class StepHandlerResult:
    """Result returned by a deterministic or effectful step handler."""

    to_state: str
    reason: str
    result_ref: str | None = None
    candidate_id_out: str | None = None
    evidence_refs: list[str] | None = None


@dataclass(frozen=True)
class ThreadClassification:
    status: Literal["consistent", "compatible", "incompatible"]
    reason: str
    rebuild_target: str | None = None


class GraphState(TypedDict):
    entity_type: str
    entity_id: str
    project_id: str
    state: str
    entity_version: int
    current_step_id: str | None
    generation: int
    last_node: str | None
    retry_signal: str | None
    halted: bool


StepHandlerFn = Callable[[sqlite3.Connection, str, str, str], StepHandlerResult]


class WorkflowEngine(Protocol):
    """Engine interface decoupling ATLAS from LangGraph internals (ADR-006 §8)."""

    def start_task(self, task_id: str) -> dict[str, Any]: ...
    def resume_task(self, task_id: str) -> dict[str, Any]: ...
    def pause_task(self, task_id: str, *, actor: str = "user") -> dict[str, Any]: ...
    def cancel_task(self, task_id: str, *, actor: str = "user") -> dict[str, Any]: ...
    def rebuild_task(self, task_id: str, *, actor: str = "user") -> dict[str, Any]: ...


def _fail_closed_handler(
    conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str
) -> StepHandlerResult:
    """Default production node handler for unbuilt Phase 2/4/5 capabilities (ATLAS-017)."""
    raise CapabilityUnavailableError("capability_unavailable")


WRITER_NODES: frozenset[str] = frozenset({"implement", "fix"})


class LangGraphWorkflowEngine:
    """Executes Task and Phase state graphs with StrictJsonSerializer and ADR-006 guarantees."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        step_handlers: dict[str, StepHandlerFn] | None = None,
        completion_handlers: dict[str, Callable[[ dict[str, Any]], StepHandlerResult]] | None = None,
    ) -> None:
        self.db = db
        self.serde = StrictJsonSerializer()
        self._step_handlers: dict[str, StepHandlerFn] = dict(step_handlers or {})
        self._completion_handlers: dict[
            str, Callable[[dict[str, Any]], StepHandlerResult]
        ] = dict(completion_handlers or {})
        self._crash_hook: Callable[[str], None] | None = None

    def set_crash_hook(self, hook: Callable[[str], None] | None) -> None:
        """Install an optional fault-injection hook for crash-window tests (ADR-006 §3.5)."""
        self._crash_hook = hook

    def _trigger_hook(self, point: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(point)

    def _open_checkpointer(self) -> tuple[sqlite3.Connection, SqliteSaver]:
        conn = sqlite3.connect(
            str(self.db.graph_db_path),
            timeout=5.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        ensure_secure_file(self.db.graph_db_path)
        saver = SqliteSaver(conn, serde=self.serde)
        saver.setup()
        return conn, saver

    @staticmethod
    def thread_id(entity_type: str, entity_id: str, generation: int) -> str:
        return f"{entity_type}:{entity_id}:g{generation}"

    # ------------------------------------------------------------------
    # ADR-006 §3: Two-node step completion contract (X_begin / X_run)
    # ------------------------------------------------------------------

    def execute_step_begin(self, state: GraphState, node_name: str) -> GraphState:
        """X_begin (no external effects): allocate or reuse the single unfinished workflow_steps row."""
        entity_type = state["entity_type"]
        entity_id = state["entity_id"]
        table = "tasks" if entity_type == "task" else "phases"

        with self.db.begin_immediate() as conn:
            entity_row = conn.execute(
                f"SELECT id, project_id, state, version, generation FROM {table} WHERE id = ?;",
                (entity_id,),
            ).fetchone()
            if not entity_row:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")

            project_id = str(entity_row["project_id"])
            entry_version = int(entity_row["version"])
            generation = int(entity_row["generation"])
            t_id = self.thread_id(entity_type, entity_id, generation)

            existing = conn.execute(
                """
                SELECT step_id
                FROM workflow_steps
                WHERE entity_id = ?
                  AND node = ?
                  AND status IN ('started', 'effects_settled')
                LIMIT 1;
                """,
                (entity_id, node_name),
            ).fetchone()

            if existing:
                step_id = str(existing["step_id"])
            else:
                step_id = new_ulid()
                cand_in = None
                if entity_type == "phase":
                    c_row = conn.execute(
                        "SELECT current_candidate_id FROM phases WHERE id = ?;",
                        (entity_id,),
                    ).fetchone()
                    cand_in = c_row["current_candidate_id"] if c_row else None

                conn.execute(
                    """
                    INSERT INTO workflow_steps (
                      step_id, project_id, entity_type, entity_id, thread_id,
                      generation, node, entry_version, status, candidate_id_in, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?);
                    """,
                    (
                        step_id,
                        project_id,
                        entity_type,
                        entity_id,
                        t_id,
                        generation,
                        node_name,
                        entry_version,
                        cand_in,
                        utc_now_iso(),
                    ),
                )

        self._trigger_hook(f"{node_name}:after_begin_txn")

        return {
            **state,
            "current_step_id": step_id,
            "entity_version": entry_version,
            "generation": generation,
            "last_node": f"{node_name}_begin",
            "retry_signal": None,
        }

    def execute_step_run(self, state: GraphState, node_name: str) -> GraphState:
        """X_run: uses current_step_id from graph state, never re-deriving from current version."""
        self._trigger_hook(f"{node_name}:before_run_dispatch")

        step_id = state.get("current_step_id")
        entity_type = state["entity_type"]
        entity_id = state["entity_id"]
        table = "tasks" if entity_type == "task" else "phases"

        if not step_id:
            raise ValueError(f"X_run for {node_name} invoked without current_step_id in graph state")

        with self.db.read_conn() as rconn:
            step_row = rconn.execute(
                "SELECT * FROM workflow_steps WHERE step_id = ?;",
                (step_id,),
            ).fetchone()
            entity_row = rconn.execute(
                f"SELECT id, project_id, state, resume_to, version, generation FROM {table} WHERE id = ?;",
                (entity_id,),
            ).fetchone()

        if not step_row or not entity_row:
            raise ValueError(f"Missing workflow_step {step_id!r} or entity {entity_id!r}")

        step_status = str(step_row["status"])

        # Case 1: Already completed (e.g. crash between completion txn and checkpoint)
        if step_status == "completed":
            return {
                **state,
                "state": str(entity_row["state"]),
                "entity_version": int(entity_row["version"]),
                "current_step_id": None,
                "last_node": f"{node_name}_run",
                "retry_signal": None,
            }

        # Case 2: Abandoned step (ATLAS-L21) — dispatch no effects and never complete it
        if step_status == "abandoned":
            disposition = str(step_row["retry_disposition"] or "deny")
            preserve_done = int(step_row["preserve_reset_done"] or 0) == 1
            is_writer = node_name in WRITER_NODES
            can_retry = (disposition == "allow") or (
                disposition == "require_preserve_reset" and (not is_writer or preserve_done)
            )
            if can_retry:
                return {
                    **state,
                    "current_step_id": None,
                    "last_node": f"{node_name}_run",
                    "retry_signal": "retry_begin",
                }
            # Otherwise transition to BLOCKED(retry_not_permitted)
            with self.db.begin_immediate() as conn:
                if entity_type == "task":
                    apply_task_transition(
                        conn,
                        task_id=entity_id,
                        to_state=TaskState.BLOCKED.value,
                        reason="Abandoned step retry not permitted",
                        actor="orchestrator",
                        blocked_reason="retry_not_permitted",
                    )
                else:
                    apply_phase_transition(
                        conn,
                        phase_id=entity_id,
                        to_state=PhaseState.BLOCKED.value,
                        reason="Abandoned step retry not permitted",
                        actor="orchestrator",
                        blocked_reason="retry_not_permitted",
                    )
                updated = conn.execute(
                    f"SELECT state, version FROM {table} WHERE id = ?;", (entity_id,)
                ).fetchone()
            return {
                **state,
                "state": str(updated["state"]),
                "entity_version": int(updated["version"]),
                "current_step_id": None,
                "last_node": f"{node_name}_run",
                "retry_signal": "blocked",
                "halted": True,
            }

        # Case 3: Step is 'started' or 'effects_settled' -> run handler keyed by step_id
        handler = self._step_handlers.get(node_name, _fail_closed_handler)
        try:
            with self.db.read_conn() as handler_conn:
                result = handler(
                    handler_conn,
                    str(entity_row["project_id"]),
                    entity_id,
                    step_id,
                )
        except CapabilityUnavailableError as exc:
            blocked_code = str(exc) or "capability_unavailable"
            with self.db.begin_immediate() as conn:
                conn.execute(
                    """
                    UPDATE workflow_steps
                    SET status = 'abandoned',
                        abandon_reason = ?,
                        retry_disposition = 'deny',
                        completed_at = ?
                    WHERE step_id = ? AND status IN ('started', 'effects_settled');
                    """,
                    (blocked_code, utc_now_iso(), step_id),
                )
                if entity_type == "task":
                    _, new_v = apply_task_transition(
                        conn,
                        task_id=entity_id,
                        to_state=TaskState.BLOCKED.value,
                        reason=f"Capability unavailable in node {node_name}",
                        actor="orchestrator",
                        blocked_reason=blocked_code,
                    )
                    new_st = TaskState.BLOCKED.value
                else:
                    _, new_v = apply_phase_transition(
                        conn,
                        phase_id=entity_id,
                        to_state=PhaseState.BLOCKED.value,
                        reason=f"Capability unavailable in node {node_name}",
                        actor="orchestrator",
                        blocked_reason=blocked_code,
                    )
                    new_st = PhaseState.BLOCKED.value
            return {
                **state,
                "state": new_st,
                "entity_version": new_v,
                "current_step_id": None,
                "last_node": f"{node_name}_run",
                "retry_signal": "blocked",
                "halted": True,
            }
        except DispatchFencedError:
            # Pause/cancel fence prevented dispatch; halt invocation cleanly
            with self.db.read_conn() as fconn:
                cur_ent = fconn.execute(
                    f"SELECT state, version FROM {table} WHERE id = ?;", (entity_id,)
                ).fetchone()
            return {
                **state,
                "state": str(cur_ent["state"]),
                "entity_version": int(cur_ent["version"]),
                "last_node": f"{node_name}_run",
                "retry_signal": "fenced",
                "halted": True,
            }

        self._trigger_hook(f"{node_name}:after_effect_before_completion")

        # Commit ONE completion transaction: result refs + outbox confirm + FSM transition + step completed
        with self.db.begin_immediate() as conn:
            step_check = conn.execute(
                "SELECT status FROM workflow_steps WHERE step_id = ?;", (step_id,)
            ).fetchone()
            if not step_check or step_check["status"] not in ("started", "effects_settled"):
                raise RuntimeError(
                    f"Cannot complete step {step_id}: status is {step_check['status'] if step_check else 'missing'}"
                )

            cur_ent = conn.execute(
                f"SELECT state, resume_to, version FROM {table} WHERE id = ?;",
                (entity_id,),
            ).fetchone()
            cur_state = str(cur_ent["state"])
            is_overlay = cur_state in ("PAUSED", "BLOCKED") and cur_ent["resume_to"] is not None

            # Confirm any in_flight outbox op keyed to this step_id
            conn.execute(
                """
                UPDATE outbox_ops
                SET status = 'confirmed', updated_at = ?
                WHERE step_id = ? AND status IN ('pending', 'in_flight');
                """,
                (utc_now_iso(), step_id),
            )

            if entity_type == "task":
                transition_id, new_version = apply_task_transition(
                    conn,
                    task_id=entity_id,
                    to_state=result.to_state,
                    reason=result.reason,
                    actor="orchestrator",
                    evidence_refs=result.evidence_refs,
                    underlying=is_overlay,
                )
            else:
                transition_id, new_version = apply_phase_transition(
                    conn,
                    phase_id=entity_id,
                    to_state=result.to_state,
                    reason=result.reason,
                    actor="orchestrator",
                    evidence_refs=result.evidence_refs,
                    underlying=is_overlay,
                )

            cur = conn.execute(
                """
                UPDATE workflow_steps
                SET status = 'completed',
                    result_ref = ?,
                    candidate_id_out = ?,
                    transition_id = ?,
                    completed_at = ?
                WHERE step_id = ? AND status IN ('started', 'effects_settled');
                """,
                (
                    result.result_ref,
                    result.candidate_id_out,
                    transition_id,
                    utc_now_iso(),
                    step_id,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Step {step_id} completion failed status check")

            final_ent = conn.execute(
                f"SELECT state, version FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()

        self._trigger_hook(f"{node_name}:after_completion_before_checkpoint")

        final_state = str(final_ent["state"])
        halted = final_state in (
            "PAUSED",
            "BLOCKED",
            "AWAITING_APPROVAL",
            "AWAITING_MANUAL",
            "COMPLETED",
            "CANCELLED",
            "FAILED",
        )
        return {
            **state,
            "state": final_state,
            "entity_version": int(final_ent["version"]),
            "current_step_id": None,
            "last_node": f"{node_name}_run",
            "retry_signal": None,
            "halted": halted,
        }

    # ------------------------------------------------------------------
    # ADR-006 §4: Serialized pause/cancel fence and dispatch reservation
    # ------------------------------------------------------------------

    def reserve_execution_dispatch(
        self,
        *,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        step_id: str,
        kind: Literal["agent", "test", "git", "gh"] = "agent",
        effect_class: Literal["ordinary", "safety"] = "ordinary",
        agent_id: str | None = None,
        role: str | None = None,
        model: str | None = None,
        provider: str = "anthropic",
        model_family: str = "anthropic",
        agent_revision: int = 1,
        argv: list[str] | None = None,
    ) -> str:
        """Check fence and insert execution reservation ('launching') in ONE BEGIN IMMEDIATE txn."""
        table = "tasks" if entity_type == "task" else "phases"
        with self.db.begin_immediate() as conn:
            ent = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()
            if not ent:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")

            if effect_class == "ordinary" and (
                ent["pause_requested_at"] is not None or ent["cancel_requested_at"] is not None
            ):
                raise DispatchFencedError(
                    f"Ordinary execution dispatch rejected: fence active on {entity_type} {entity_id}"
                )

            existing = conn.execute(
                "SELECT id, status FROM executions WHERE idempotency_key = ?;",
                (step_id,),
            ).fetchone()
            if existing:
                return str(existing["id"])

            exec_id = new_ulid()
            project_id = str(ent["project_id"])
            task_id = entity_id if entity_type == "task" else str(ent["task_id"])
            phase_id = None if entity_type == "task" else entity_id
            candidate_id = None if entity_type == "task" else ent["current_candidate_id"]
            now = utc_now_iso()

            conn.execute(
                """
                INSERT INTO executions (
                  id, project_id, task_id, phase_id, candidate_id, step_id, idempotency_key,
                  kind, agent_id, role, model, agent_revision, provider, model_family,
                  status, retry_count, argv_json, correlation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'launching', 0, ?, ?, ?);
                """,
                (
                    exec_id,
                    project_id,
                    task_id,
                    phase_id,
                    candidate_id,
                    step_id,
                    step_id,
                    kind,
                    agent_id,
                    role,
                    model,
                    agent_revision if kind == "agent" else None,
                    provider if kind == "agent" else None,
                    model_family if kind == "agent" else None,
                    json.dumps(argv or []),
                    new_ulid(),
                    now,
                ),
            )
            return exec_id

    def reserve_outbox_dispatch(
        self,
        *,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        step_id: str,
        op: str,
        intent: dict[str, Any],
        effect_class: Literal["ordinary", "safety"] = "ordinary",
    ) -> str:
        """Check fence and insert outbox_op reservation ('in_flight') in ONE BEGIN IMMEDIATE txn."""
        table = "tasks" if entity_type == "task" else "phases"
        with self.db.begin_immediate() as conn:
            ent = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()
            if not ent:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")

            if effect_class == "ordinary" and (
                ent["pause_requested_at"] is not None or ent["cancel_requested_at"] is not None
            ):
                raise DispatchFencedError(
                    f"Ordinary outbox dispatch ({op}) rejected: fence active on {entity_type} {entity_id}"
                )

            idem_key = f"{step_id}:{op}"
            existing = conn.execute(
                "SELECT id FROM outbox_ops WHERE idempotency_key = ?;",
                (idem_key,),
            ).fetchone()
            if existing:
                return str(existing["id"])

            op_id = new_ulid()
            project_id = str(ent["project_id"])
            phase_id = None if entity_type == "task" else entity_id
            candidate_id = None if entity_type == "task" else ent["current_candidate_id"]
            now = utc_now_iso()

            conn.execute(
                """
                INSERT INTO outbox_ops (
                  id, project_id, phase_id, candidate_id, step_id, op,
                  idempotency_key, intent_json, status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', 1, ?, ?);
                """,
                (
                    op_id,
                    project_id,
                    phase_id,
                    candidate_id,
                    step_id,
                    op,
                    idem_key,
                    json.dumps(intent),
                    now,
                    now,
                ),
            )
            return op_id

    def request_pause(
        self,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        *,
        actor: str = "user",
        reason: str = "Paused by user",
        withdraw_remote_merge_fn: Callable[[sqlite3.Connection, str], bool] | None = None,
    ) -> dict[str, Any]:
        """Execute the 4-step Pause Protocol (ADR-006 §4):
        1. Set fence in BEGIN IMMEDIATE.
        2. Settle reservations committed before the fence.
        3. Withdraw any queued/armed remote merge (safety class, permitted under fence).
        4. Commit PAUSED with resume_to.
        """
        table = "tasks" if entity_type == "task" else "phases"
        now = utc_now_iso()

        # Step 1: Set the fence
        with self.db.begin_immediate() as conn:
            prev = conn.execute(
                f"SELECT pause_requested_at FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()
            if not prev:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")
            prev_fence = prev["pause_requested_at"]
            conn.execute(
                f"UPDATE {table} SET pause_requested_at = ?, updated_at = ? WHERE id = ?;",
                (now, now, entity_id),
            )

        try:
            return self._settle_and_pause(
                entity_type,
                entity_id,
                actor=actor,
                reason=reason,
                now=now,
                withdraw_remote_merge_fn=withdraw_remote_merge_fn,
            )
        except Exception:
            # The pause was rejected (e.g. terminal state, guard failure): restore the prior fence
            # so a failed request cannot leave ordinary dispatch fenced with no PAUSED to resume from.
            with self.db.begin_immediate() as conn:
                conn.execute(
                    f"UPDATE {table} SET pause_requested_at = ? WHERE id = ? AND pause_requested_at = ?;",
                    (prev_fence, entity_id, now),
                )
            raise

    def _settle_and_pause(
        self,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        *,
        actor: str,
        reason: str,
        now: str,
        withdraw_remote_merge_fn: Callable[[sqlite3.Connection, str], bool] | None,
    ) -> dict[str, Any]:
        """Pause Protocol steps 2-4, run after the fence is committed."""
        table = "tasks" if entity_type == "task" else "phases"

        # Step 2 & 3: Settle live reservations & withdraw remote merge if needed
        with self.db.begin_immediate() as conn:
            ent = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()
            if not ent:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")

            id_col = "task_id" if entity_type == "task" else "phase_id"
            conn.execute(
                f"""
                UPDATE executions
                SET status = 'interrupted', finished_at = ?
                WHERE {id_col} = ? AND status IN ('launching', 'running');
                """,
                (now, entity_id),
            )

            cur_state = str(ent["state"])
            if cur_state == "PAUSED":
                return {"id": entity_id, "state": "PAUSED", "resume_to": ent["resume_to"]}

            if entity_type == "phase" and cur_state in (
                PhaseState.MERGE_PENDING_REMOTE.value,
                PhaseState.CANCELLING_REMOTE_MERGE.value,
            ):
                # Safety-class withdrawal permitted under fence
                withdrawn_ok = True
                if withdraw_remote_merge_fn is not None:
                    withdrawn_ok = withdraw_remote_merge_fn(conn, entity_id)
                if not withdrawn_ok:
                    apply_phase_transition(
                        conn,
                        phase_id=entity_id,
                        to_state=PhaseState.BLOCKED.value,
                        reason="Remote merge request could not be withdrawn before pause",
                        actor=actor,
                        blocked_reason="remote_merge_not_withdrawn",
                    )
                    return {
                        "id": entity_id,
                        "state": PhaseState.BLOCKED.value,
                        "blocked_reason": "remote_merge_not_withdrawn",
                    }
                conn.execute(
                    "UPDATE phases SET remote_merge_withdrawn = 1, updated_at = ? WHERE id = ?;",
                    (now, entity_id),
                )

            # Step 4: Commit PAUSED with resume_to
            if entity_type == "task":
                apply_task_transition(
                    conn,
                    task_id=entity_id,
                    to_state=TaskState.PAUSED.value,
                    reason=reason,
                    actor=actor,
                )
            else:
                apply_phase_transition(
                    conn,
                    phase_id=entity_id,
                    to_state=PhaseState.PAUSED.value,
                    reason=reason,
                    actor=actor,
                )

            updated = conn.execute(
                f"SELECT id, state, resume_to, version FROM {table} WHERE id = ?;",
                (entity_id,),
            ).fetchone()
            return dict(updated)

    def request_cancel(
        self,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        *,
        actor: str = "user",
        reason: str = "Cancelled by user",
    ) -> dict[str, Any]:
        """Set cancel fence, settle reservations, and commit CANCELLED."""
        table = "tasks" if entity_type == "task" else "phases"
        now = utc_now_iso()
        with self.db.begin_immediate() as conn:
            conn.execute(
                f"UPDATE {table} SET cancel_requested_at = ?, updated_at = ? WHERE id = ?;",
                (now, now, entity_id),
            )
            id_col = "task_id" if entity_type == "task" else "phase_id"
            conn.execute(
                f"""
                UPDATE executions
                SET status = 'cancelled', finished_at = ?
                WHERE {id_col} = ? AND status IN ('launching', 'running');
                """,
                (now, entity_id),
            )
            if entity_type == "task":
                apply_task_transition(
                    conn,
                    task_id=entity_id,
                    to_state=TaskState.CANCELLED.value,
                    reason=reason,
                    actor=actor,
                )
            else:
                apply_phase_transition(
                    conn,
                    phase_id=entity_id,
                    to_state=PhaseState.CANCELLED.value,
                    reason=reason,
                    actor=actor,
                )
            updated = conn.execute(
                f"SELECT id, state, resume_to, version FROM {table} WHERE id = ?;",
                (entity_id,),
            ).fetchone()
            return dict(updated)

    # ------------------------------------------------------------------
    # ADR-006 §5.1: Startup resolution of unfinished workflow_steps
    # ------------------------------------------------------------------

    def resolve_unfinished_steps(self) -> list[dict[str, Any]]:
        """Resolve workflow_steps in 'started' or 'effects_settled' before startup PAUSED overlay (ADR-006 §5.1)."""
        resolved_actions: list[dict[str, Any]] = []
        with self.db.begin_immediate() as conn:
            steps = conn.execute(
                """
                SELECT * FROM workflow_steps
                WHERE status IN ('started', 'effects_settled')
                ORDER BY created_at ASC;
                """
            ).fetchall()

            for step in steps:
                step_id = str(step["step_id"])
                entity_type = str(step["entity_type"])
                entity_id = str(step["entity_id"])
                node_name = str(step["node"])
                table = "tasks" if entity_type == "task" else "phases"

                ent = conn.execute(
                    f"SELECT id, state, resume_to, version FROM {table} WHERE id = ?;",
                    (entity_id,),
                ).fetchone()
                if not ent:
                    continue

                exec_row = conn.execute(
                    "SELECT * FROM executions WHERE step_id = ? ORDER BY created_at DESC LIMIT 1;",
                    (step_id,),
                ).fetchone()
                outbox_row = conn.execute(
                    "SELECT * FROM outbox_ops WHERE step_id = ? ORDER BY created_at DESC LIMIT 1;",
                    (step_id,),
                ).fetchone()

                # 1. No dispatch reservation exists (crash before dispatch) -> leave 'started'
                if not exec_row and not outbox_row:
                    resolved_actions.append(
                        {"step_id": step_id, "action": "left_started_no_reservation"}
                    )
                    continue

                # 2. Outbox op still reconciling / abandoned -> outcome unknown -> BLOCKED(effect_outcome_unknown)
                if outbox_row and outbox_row["status"] in ("reconciling", "abandoned"):
                    self._block_outcome_unknown(
                        conn, entity_type, entity_id, str(ent["state"]),
                        f"Step {step_id} outbox outcome unknown",
                    )
                    resolved_actions.append(
                        {"step_id": step_id, "action": "blocked_effect_outcome_unknown"}
                    )
                    continue

                # 3. Execution succeeded (with result_json) OR outbox op confirmed -> complete step now (actor='recovery')
                exec_succeeded = (
                    exec_row is not None
                    and exec_row["status"] == "succeeded"
                    and exec_row["result_json"] is not None
                )
                outbox_confirmed = (
                    outbox_row is not None and outbox_row["status"] == "confirmed"
                )
                if exec_succeeded or outbox_confirmed:
                    payload: dict[str, Any] = {}
                    if exec_succeeded and exec_row["result_json"]:
                        payload = json.loads(exec_row["result_json"])
                    elif outbox_confirmed and outbox_row["intent_json"]:
                        payload = json.loads(outbox_row["intent_json"])

                    comp_fn = self._completion_handlers.get(node_name)
                    comp_res: StepHandlerResult | None = None
                    if comp_fn is not None:
                        comp_res = comp_fn(payload)
                    elif payload.get("to_state"):
                        comp_res = StepHandlerResult(
                            to_state=str(payload["to_state"]),
                            reason=str(payload.get("reason", f"Completed {node_name} in recovery")),
                            result_ref=payload.get("result_ref"),
                            candidate_id_out=payload.get("candidate_id_out"),
                        )

                    cur_state = str(ent["state"])
                    is_overlay = cur_state in ("PAUSED", "BLOCKED") and ent["resume_to"] is not None

                    # One unresolvable step must not roll back recovery for every other step,
                    # so block just this entity and leave the step for the operator.
                    t_id: str | None = None
                    if comp_res is not None:
                        try:
                            if entity_type == "task":
                                t_id, _ = apply_task_transition(
                                    conn,
                                    task_id=entity_id,
                                    to_state=comp_res.to_state,
                                    reason=comp_res.reason,
                                    actor="recovery",
                                    evidence_refs=comp_res.evidence_refs,
                                    underlying=is_overlay,
                                )
                            else:
                                t_id, _ = apply_phase_transition(
                                    conn,
                                    phase_id=entity_id,
                                    to_state=comp_res.to_state,
                                    reason=comp_res.reason,
                                    actor="recovery",
                                    evidence_refs=comp_res.evidence_refs,
                                    underlying=is_overlay,
                                )
                        except (InvalidTransitionError, GuardFailedError):
                            t_id = None
                    if comp_res is None or t_id is None:
                        self._block_outcome_unknown(
                            conn, entity_type, entity_id, cur_state,
                            f"Step {step_id} result could not be applied in recovery",
                        )
                        resolved_actions.append(
                            {"step_id": step_id, "action": "blocked_effect_outcome_unknown"}
                        )
                        continue

                    conn.execute(
                        """
                        UPDATE workflow_steps
                        SET status = 'completed',
                            result_ref = ?,
                            candidate_id_out = ?,
                            transition_id = ?,
                            completed_at = ?
                        WHERE step_id = ?;
                        """,
                        (
                            comp_res.result_ref,
                            comp_res.candidate_id_out,
                            t_id,
                            utc_now_iso(),
                            step_id,
                        ),
                    )
                    resolved_actions.append({"step_id": step_id, "action": "completed_by_recovery"})
                    continue

                # 4. Execution interrupted/failed or outbox failed -> mark step 'abandoned'
                is_writer = node_name in WRITER_NODES
                disposition = "require_preserve_reset" if is_writer else "allow"
                conn.execute(
                    """
                    UPDATE workflow_steps
                    SET status = 'abandoned',
                        abandon_reason = 'execution_interrupted_or_failed',
                        retry_disposition = ?,
                        preserve_reset_done = ?,
                        completed_at = ?
                    WHERE step_id = ?;
                    """,
                    (disposition, 1 if is_writer else 0, utc_now_iso(), step_id),
                )
                resolved_actions.append(
                    {"step_id": step_id, "action": "abandoned", "disposition": disposition}
                )

        return resolved_actions

    @staticmethod
    def _block_outcome_unknown(
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        cur_state: str,
        reason: str,
    ) -> None:
        """Move an entity to BLOCKED(effect_outcome_unknown) unless it is already BLOCKED or terminal."""
        allowed = is_task_transition_allowed if entity_type == "task" else is_phase_transition_allowed
        if cur_state == "BLOCKED" or not allowed(cur_state, "BLOCKED"):
            return
        if entity_type == "task":
            apply_task_transition(
                conn,
                task_id=entity_id,
                to_state=TaskState.BLOCKED.value,
                reason=reason,
                actor="recovery",
                blocked_reason="effect_outcome_unknown",
            )
        else:
            apply_phase_transition(
                conn,
                phase_id=entity_id,
                to_state=PhaseState.BLOCKED.value,
                reason=reason,
                actor="recovery",
                blocked_reason="effect_outcome_unknown",
            )

    # ------------------------------------------------------------------
    # ADR-006 §5.2: Startup thread classification
    # ------------------------------------------------------------------

    def classify_thread(
        self, entity_type: Literal["task", "phase"], entity_id: str
    ) -> ThreadClassification:
        """Classify checkpoint against app DB transitions (ADR-006 §5.2).

        If incompatible, transitions the entity to BLOCKED(engine_state_divergence)
        and records rebuild_target = last non-overlay state.
        """
        table = "tasks" if entity_type == "task" else "phases"
        with self.db.read_conn() as conn:
            ent = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()
            if not ent:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")

            app_version = int(ent["version"])
            app_gen = int(ent["generation"])
            app_state = str(ent["state"])
            app_resume_to = ent["resume_to"]

        t_id = self.thread_id(entity_type, entity_id, app_gen)
        cfg: RunnableConfig = {"configurable": {"thread_id": t_id}}
        g_conn, saver = self._open_checkpointer()
        try:
            tup = saver.get_tuple(cfg)
        finally:
            g_conn.close()

        # Determine last non-overlay state for rebuild_target
        rebuild_target = str(app_resume_to) if app_state in ("PAUSED", "BLOCKED") and app_resume_to else app_state

        if tup is None:
            # An entity at version 0 (just CREATED/PENDING, never started a graph thread) is consistent
            if app_version == 0 or (
                app_version == 1
                and app_state == "PAUSED"
                and app_resume_to in ("CREATED", "PENDING")
            ):
                return ThreadClassification("consistent", "Entity has not started a graph thread yet")
            return self._mark_diverged(
                entity_type,
                entity_id,
                rebuild_target,
                "Missing LangGraph checkpoint for active entity",
            )

        channel_values: dict[str, Any] = tup.checkpoint.get("channel_values", {})
        v_c = channel_values.get("entity_version")
        cp_gen = channel_values.get("generation")
        cp_step_id = channel_values.get("current_step_id")

        if not isinstance(v_c, int) or not isinstance(cp_gen, int):
            return self._mark_diverged(
                entity_type,
                entity_id,
                rebuild_target,
                "Checkpoint missing integer entity_version or generation",
            )

        if cp_gen != app_gen:
            return self._mark_diverged(
                entity_type,
                entity_id,
                rebuild_target,
                f"Generation mismatch: checkpoint={cp_gen}, app={app_gen}",
            )

        if v_c > app_version:
            return self._mark_diverged(
                entity_type,
                entity_id,
                rebuild_target,
                f"Checkpoint version {v_c} is ahead of app DB version {app_version}",
            )

        with self.db.read_conn() as conn:
            if cp_step_id is not None:
                step_row = conn.execute(
                    "SELECT entity_id, candidate_id_in FROM workflow_steps WHERE step_id = ?;",
                    (cp_step_id,),
                ).fetchone()
                if not step_row or step_row["entity_id"] != entity_id:
                    return self._mark_diverged(
                        entity_type,
                        entity_id,
                        rebuild_target,
                        f"Checkpoint current_step_id {cp_step_id!r} unknown or belongs to another entity",
                    )
                if entity_type == "phase" and step_row["candidate_id_in"] is not None:
                    if ent["current_candidate_id"] and step_row["candidate_id_in"] != ent["current_candidate_id"]:
                        return self._mark_diverged(
                            entity_type,
                            entity_id,
                            rebuild_target,
                            "Checkpoint current_step_id belongs to another candidate",
                        )

            all_transitions = conn.execute(
                """
                SELECT id, from_state, to_state, actor, underlying
                FROM transitions
                WHERE entity_type = ? AND entity_id = ? AND from_state IS NOT NULL
                ORDER BY rowid ASC;
                """,
                (entity_type, entity_id),
            ).fetchall()

            # Transitions after v_c. Each FSM transition increments version by 1 from 0; the
            # creation record (from_state NULL) is written without a version bump, so it is excluded.
            later_transitions = all_transitions[v_c:]
            if not later_transitions:
                return ThreadClassification("consistent", "Checkpoint version matches app DB")

            # Find transition_id of cp_step_id if completed
            cp_step_transition_id: str | None = None
            if cp_step_id is not None:
                s_row = conn.execute(
                    "SELECT transition_id FROM workflow_steps WHERE step_id = ?;",
                    (cp_step_id,),
                ).fetchone()
                if s_row and s_row["transition_id"]:
                    cp_step_transition_id = str(s_row["transition_id"])

            for tr in later_transitions:
                tr_id = str(tr["id"])
                tr_actor = str(tr["actor"])
                tr_to = str(tr["to_state"])
                tr_from = str(tr["from_state"] or "")

                if cp_step_transition_id and tr_id == cp_step_transition_id:
                    continue

                is_overlay_actor = tr_actor in ("recovery", "user") or tr_actor.startswith("user:")
                is_overlay_move = tr_to in ("PAUSED", "BLOCKED", "AWAITING_APPROVAL") or tr_from in (
                    "PAUSED",
                    "BLOCKED",
                    "AWAITING_APPROVAL",
                )
                if is_overlay_actor and is_overlay_move:
                    continue

                return self._mark_diverged(
                    entity_type,
                    entity_id,
                    rebuild_target,
                    f"Incompatible transition {tr_id} ({tr_from} -> {tr_to} by {tr_actor}) after checkpoint v={v_c}",
                )

        return ThreadClassification("compatible", "App-ahead transitions are own-step completion and/or overlays")

    def _mark_diverged(
        self,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        rebuild_target: str,
        detail: str,
    ) -> ThreadClassification:
        table = "tasks" if entity_type == "task" else "phases"
        with self.db.begin_immediate() as conn:
            ent = conn.execute(
                f"SELECT state FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()
            if ent and ent["state"] != "BLOCKED":
                if entity_type == "task":
                    apply_task_transition(
                        conn,
                        task_id=entity_id,
                        to_state=TaskState.BLOCKED.value,
                        reason=detail,
                        actor="recovery",
                        blocked_reason="engine_state_divergence",
                    )
                else:
                    apply_phase_transition(
                        conn,
                        phase_id=entity_id,
                        to_state=PhaseState.BLOCKED.value,
                        reason=detail,
                        actor="recovery",
                        blocked_reason="engine_state_divergence",
                    )
            conn.execute(
                f"UPDATE {table} SET rebuild_target = ?, blocked_reason = 'engine_state_divergence', updated_at = ? WHERE id = ?;",
                (rebuild_target, utc_now_iso(), entity_id),
            )
        return ThreadClassification("incompatible", detail, rebuild_target=rebuild_target)

    # ------------------------------------------------------------------
    # ADR-006 §5.3: Audited user-triggered thread rebuild
    # ------------------------------------------------------------------

    def rebuild_thread(
        self,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        """Audited rebuild from BLOCKED(engine_state_divergence) (ADR-006 §5.3)."""
        table = "tasks" if entity_type == "task" else "phases"
        id_col = "task_id" if entity_type == "task" else "phase_id"

        with self.db.begin_immediate() as conn:
            ent = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?;", (entity_id,)
            ).fetchone()
            if not ent:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")

            if ent["state"] != "BLOCKED" or ent["blocked_reason"] != "engine_state_divergence":
                raise RebuildPreconditionError(
                    f"Rebuild requires BLOCKED(engine_state_divergence); got state={ent['state']} reason={ent['blocked_reason']}"
                )

            # Precondition 1: no unfinished step with a dispatch reservation or in effects_settled
            unfinished = conn.execute(
                """
                SELECT step_id, status FROM workflow_steps
                WHERE entity_id = ? AND status IN ('started', 'effects_settled');
                """,
                (entity_id,),
            ).fetchall()
            for u in unfinished:
                if u["status"] == "effects_settled":
                    raise RebuildPreconditionError(
                        f"Step {u['step_id']} is still in effects_settled"
                    )
                has_exec = conn.execute(
                    "SELECT 1 FROM executions WHERE step_id = ? LIMIT 1;",
                    (u["step_id"],),
                ).fetchone()
                has_outbox = conn.execute(
                    "SELECT 1 FROM outbox_ops WHERE step_id = ? LIMIT 1;",
                    (u["step_id"],),
                ).fetchone()
                if has_exec or has_outbox:
                    raise RebuildPreconditionError(
                        f"Unfinished step {u['step_id']} still has a dispatch reservation; run step 5.1 resolution first"
                    )
                # Abandon reservationless started step from old generation
                conn.execute(
                    """
                    UPDATE workflow_steps
                    SET status = 'abandoned', abandon_reason = 'superseded_by_rebuild',
                        retry_disposition = 'allow', completed_at = ?
                    WHERE step_id = ?;
                    """,
                    (utc_now_iso(), u["step_id"]),
                )

            # Precondition 2: no launching/running executions or in_flight/reconciling outbox ops
            live_exec = conn.execute(
                f"SELECT id FROM executions WHERE {id_col} = ? AND status IN ('launching', 'running') LIMIT 1;",
                (entity_id,),
            ).fetchone()
            if live_exec:
                raise RebuildPreconditionError(f"Live execution {live_exec['id']} still active")

            if entity_type == "phase":
                live_outbox = conn.execute(
                    "SELECT id FROM outbox_ops WHERE phase_id = ? AND status IN ('in_flight', 'reconciling') LIMIT 1;",
                    (entity_id,),
                ).fetchone()
                if live_outbox:
                    raise RebuildPreconditionError(f"Outbox op {live_outbox['id']} still in_flight/reconciling")

            rebuild_target = str(ent["rebuild_target"] or ent["resume_to"] or ("CREATED" if entity_type == "task" else "PENDING"))
            old_gen = int(ent["generation"])
            new_gen = old_gen + 1
            now = utc_now_iso()

            if entity_type == "task":
                _, new_ver = apply_task_transition(
                    conn,
                    task_id=entity_id,
                    to_state=rebuild_target,
                    reason=f"Audited thread rebuild g{old_gen} -> g{new_gen}",
                    actor=actor,
                )
            else:
                _, new_ver = apply_phase_transition(
                    conn,
                    phase_id=entity_id,
                    to_state=rebuild_target,
                    reason=f"Audited thread rebuild g{old_gen} -> g{new_gen}",
                    actor=actor,
                )

            conn.execute(
                f"""
                UPDATE {table}
                SET generation = ?, rebuild_target = NULL, blocked_reason = NULL, updated_at = ?
                WHERE id = ?;
                """,
                (new_gen, now, entity_id),
            )

            conn.execute(
                """
                INSERT INTO audit_log (id, project_id, actor, action, detail_json, correlation_id, at)
                VALUES (?, ?, ?, 'thread_rebuild', ?, ?, ?);
                """,
                (
                    new_ulid(),
                    ent["project_id"],
                    actor,
                    json.dumps(
                        {
                            "entity_type": entity_type,
                            "entity_id": entity_id,
                            "old_generation": old_gen,
                            "new_generation": new_gen,
                            "rebuild_target": rebuild_target,
                        }
                    ),
                    new_ulid(),
                    now,
                ),
            )
            project_id = str(ent["project_id"])

        # Seed the new thread checkpoint at rebuild_target with current_step_id=None
        self.seed_checkpoint(
            entity_type=entity_type,
            entity_id=entity_id,
            project_id=project_id,
            state=rebuild_target,
            entity_version=new_ver,
            generation=new_gen,
            current_step_id=None,
        )

        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "state": rebuild_target,
            "generation": new_gen,
            "version": new_ver,
        }

    def seed_checkpoint(
        self,
        *,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        project_id: str,
        state: str,
        entity_version: int,
        generation: int,
        current_step_id: str | None = None,
    ) -> str:
        """Write an initial or rebuilt checkpoint in graph.db using a single-node identity StateGraph."""
        t_id = self.thread_id(entity_type, entity_id, generation)

        def _seed_node(s: GraphState) -> GraphState:
            return s

        g: Any = StateGraph(GraphState)
        g.add_node("seed", _seed_node)
        g.add_edge(START, "seed")
        g.add_edge("seed", END)

        cfg: RunnableConfig = {"configurable": {"thread_id": t_id}}
        g_conn, saver = self._open_checkpointer()
        try:
            app = g.compile(checkpointer=saver)
            app.invoke(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "project_id": project_id,
                    "state": state,
                    "entity_version": entity_version,
                    "current_step_id": current_step_id,
                    "generation": generation,
                    "last_node": "seed",
                    "retry_signal": None,
                    "halted": False,
                },
                cfg,
                durability="sync",
            )
        finally:
            g_conn.close()
        return t_id

    # ------------------------------------------------------------------
    # Graph execution for two-node step pairs (X_begin -> X_run)
    # ------------------------------------------------------------------

    def run_step_pair(
        self,
        entity_type: Literal["task", "phase"],
        entity_id: str,
        node_name: str,
    ) -> GraphState:
        """Compile and run a two-node step pair (X_begin -> X_run, with abandoned retry edge X_run -> X_begin)
        on LangGraph with durability='sync' and StrictJsonSerializer.
        """
        table = "tasks" if entity_type == "task" else "phases"
        with self.db.read_conn() as conn:
            ent = conn.execute(
                f"SELECT id, project_id, state, version, generation FROM {table} WHERE id = ?;",
                (entity_id,),
            ).fetchone()
            if not ent:
                raise EntityNotFoundError(f"{entity_type} {entity_id!r} not found")

            project_id = str(ent["project_id"])
            cur_state = str(ent["state"])
            cur_version = int(ent["version"])
            generation = int(ent["generation"])

        t_id = self.thread_id(entity_type, entity_id, generation)

        def _begin(s: GraphState) -> GraphState:
            return self.execute_step_begin(s, node_name)

        def _run(s: GraphState) -> GraphState:
            return self.execute_step_run(s, node_name)

        def _route_after_run(s: GraphState) -> str:
            if s.get("retry_signal") == "retry_begin":
                return f"{node_name}_begin"
            return END

        g: Any = StateGraph(GraphState)
        g.add_node(f"{node_name}_begin", _begin)
        g.add_node(f"{node_name}_run", _run)
        g.add_edge(START, f"{node_name}_begin")
        g.add_edge(f"{node_name}_begin", f"{node_name}_run")
        g.add_conditional_edges(
            f"{node_name}_run",
            _route_after_run,
            {f"{node_name}_begin": f"{node_name}_begin", END: END},
        )

        g_conn, saver = self._open_checkpointer()
        try:
            app = g.compile(checkpointer=saver)
            cfg: RunnableConfig = {"configurable": {"thread_id": t_id}}
            existing_tup = saver.get_tuple(cfg)
            initial_state: GraphState
            if existing_tup and existing_tup.checkpoint.get("channel_values"):
                cv = existing_tup.checkpoint["channel_values"]
                initial_state = {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "project_id": project_id,
                    "state": cur_state,
                    "entity_version": cur_version,
                    "current_step_id": cv.get("current_step_id"),
                    "generation": generation,
                    "last_node": cv.get("last_node"),
                    "retry_signal": None,
                    "halted": False,
                }
            else:
                initial_state = {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "project_id": project_id,
                    "state": cur_state,
                    "entity_version": cur_version,
                    "current_step_id": None,
                    "generation": generation,
                    "last_node": None,
                    "retry_signal": None,
                    "halted": False,
                }
            out = app.invoke(initial_state, cfg, durability="sync")
            return cast(GraphState, out)
        finally:
            g_conn.close()

    # ------------------------------------------------------------------
    # High-level Task lifecycle operations (Phase 1)
    # ------------------------------------------------------------------

    def start_task(self, task_id: str) -> dict[str, Any]:
        """Start a task from CREATED -> REPOSITORY_ANALYSIS -> run analysis step (fail-closed in Phase 1 unless handler injected)."""
        with self.db.begin_immediate() as conn:
            row = conn.execute(
                "SELECT id, project_id, state, version, generation FROM tasks WHERE id = ?;",
                (task_id,),
            ).fetchone()
            if not row:
                raise EntityNotFoundError(f"Task {task_id!r} not found")
            if row["state"] == TaskState.CREATED.value:
                apply_task_transition(
                    conn,
                    task_id=task_id,
                    to_state=TaskState.REPOSITORY_ANALYSIS.value,
                    reason="Task started by user",
                    actor="user",
                )
        state = self.run_step_pair("task", task_id, "analyse")
        return dict(state)

    def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a PAUSED task back to its recorded resume_to state."""
        with self.db.begin_immediate() as conn:
            row = conn.execute(
                "SELECT id, project_id, state, resume_to, version, generation FROM tasks WHERE id = ?;",
                (task_id,),
            ).fetchone()
            if not row:
                raise EntityNotFoundError(f"Task {task_id!r} not found")
            if row["state"] != TaskState.PAUSED.value:
                raise InvalidTransitionError(f"Task {task_id!r} is in state {row['state']}, not PAUSED")
            target = str(row["resume_to"] or TaskState.CREATED.value)
            conn.execute(
                "UPDATE tasks SET pause_requested_at = NULL WHERE id = ?;",
                (task_id,),
            )
            _, new_ver = apply_task_transition(
                conn,
                task_id=task_id,
                to_state=target,
                reason=f"Resumed by user to {target}",
                actor="user",
            )
            project_id = str(row["project_id"])
            gen = int(row["generation"])

        self.seed_checkpoint(
            entity_type="task",
            entity_id=task_id,
            project_id=project_id,
            state=target,
            entity_version=new_ver,
            generation=gen,
        )
        return {"id": task_id, "state": target, "version": new_ver, "generation": gen}

    def pause_task(self, task_id: str, *, actor: str = "user") -> dict[str, Any]:
        return self.request_pause("task", task_id, actor=actor)

    def cancel_task(self, task_id: str, *, actor: str = "user") -> dict[str, Any]:
        return self.request_cancel("task", task_id, actor=actor)

    def rebuild_task(self, task_id: str, *, actor: str = "user") -> dict[str, Any]:
        return self.rebuild_thread("task", task_id, actor=actor)
