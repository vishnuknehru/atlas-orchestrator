"""Slice 1.0 & 1.4b tests: StrictJsonSerializer, crash windows, pause fence, classification, rebuild, and zero egress."""

from __future__ import annotations

import json
import os
import pickle
import socket
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from atlas.db.connection import DatabaseManager
from atlas.engine.serde import StrictJsonSerializer, StrictSerializationError
from atlas.engine.workflow import (
    DispatchFencedError,
    LangGraphWorkflowEngine,
    StepHandlerResult,
)
from atlas.fsm.states import TaskState
from atlas.fsm.transitions import apply_task_transition
from tests.test_db_integrity import _seed_project_task_plan_phase


def test_strict_json_serializer_round_trip_and_hostile_rejection() -> None:
    serde = StrictJsonSerializer()

    valid_payload = {
        "entity_type": "task",
        "entity_id": "01JABCDEF",
        "entity_version": 3,
        "generation": 1,
        "current_step_id": None,
        "flags": [True, False, None, 42, 3.5],
    }
    type_tag, data = serde.dumps_typed(valid_payload)
    assert type_tag == "json"
    assert serde.loads_typed((type_tag, data)) == valid_payload

    # 1. Reject non-'json' type tags (pickle, msgpack, bytes) before decoding
    for bad_tag in ("pickle", "msgpack", "bytes", "lc_json"):
        with pytest.raises(StrictSerializationError, match="Rejected non-JSON checkpoint type tag"):
            serde.loads_typed((bad_tag, data))

    # 2. Reject hostile pickle bytes even if mislabelled as 'json'
    pickled = pickle.dumps({"a": 1})
    with pytest.raises(StrictSerializationError, match="Hostile binary payload header"):
        serde.loads_typed(("json", pickled))

    # 3. Reject msgpack extension / header bytes even if mislabelled as 'json'
    with pytest.raises(StrictSerializationError, match="Hostile binary payload header"):
        serde.loads_typed(("json", b"\x91\xc7A\x02"))

    # 4. Reject LangChain ('lc') or object constructor JSON payloads
    lc_payload = json.dumps({"lc": 1, "type": "constructor", "id": ["os", "system"]}).encode("utf-8")
    with pytest.raises(StrictSerializationError, match="Forbidden constructor key 'lc'"):
        serde.loads_typed(("json", lc_payload))

    # 5. Reject NaN / Infinity and arbitrary custom objects on dump
    with pytest.raises(StrictSerializationError):
        serde.dumps_typed({"bad": float("nan")})

    with pytest.raises(StrictSerializationError):
        serde.dumps_typed({"bad": object()})


def test_crash_windows_and_step_replay_safety(tmp_path: Path) -> None:
    """Verify all 4 crash windows in ADR-006 §3.5:
    1. X_begin txn -> checkpoint: replays and reuses unfinished step row.
    2. checkpoint -> effect dispatch: dispatches once.
    3. effect -> completion txn: reconciled by step_id in startup 5.1, commits once.
    4. completion txn -> checkpoint: X_run sees completed, returns result, no duplicate transition.
    """
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    with db.begin_immediate() as conn:
        _, tid, _, _ = _seed_project_task_plan_phase(conn)
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.REPOSITORY_ANALYSIS.value,
            reason="start",
            actor="user",
        )

    dispatch_calls: list[str] = []

    def analyse_handler(
        conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str
    ) -> StepHandlerResult:
        dispatch_calls.append(step_id)
        engine.reserve_execution_dispatch(
            entity_type="task",
            entity_id=entity_id,
            step_id=step_id,
            kind="agent",
            agent_id=None,
            role="architect",
            model="claude-sonnet-4-5",
        )
        with db.begin_immediate() as wconn:
            wconn.execute(
                """
                UPDATE executions
                SET status = 'succeeded',
                    exit_code = 0,
                    result_json = ?
                WHERE step_id = ?;
                """,
                (
                    json.dumps(
                        {
                            "to_state": TaskState.PLANNING.value,
                            "reason": "Repository analysis completed",
                            "result_ref": "blob-1",
                        }
                    ),
                    step_id,
                ),
            )
        return StepHandlerResult(
            to_state=TaskState.PLANNING.value,
            reason="Repository analysis completed",
            result_ref="blob-1",
        )

    engine = LangGraphWorkflowEngine(db, step_handlers={"analyse": analyse_handler})

    # Window 1: Crash after X_begin txn before checkpoint
    def crash_w1(point: str) -> None:
        if point == "analyse:after_begin_txn":
            raise RuntimeError("simulated_crash_w1")

    engine.set_crash_hook(crash_w1)
    with pytest.raises(RuntimeError, match="simulated_crash_w1"):
        engine.run_step_pair("task", tid, "analyse")

    with db.read_conn() as conn:
        steps_w1 = conn.execute(
            "SELECT step_id, status FROM workflow_steps WHERE entity_id = ?;", (tid,)
        ).fetchall()
    assert len(steps_w1) == 1
    first_step_id = str(steps_w1[0]["step_id"])
    assert steps_w1[0]["status"] == "started"

    # Window 2: Crash before run dispatch -> X_begin reuses the exact same step_id!
    def crash_w2(point: str) -> None:
        if point == "analyse:before_run_dispatch":
            raise RuntimeError("simulated_crash_w2")

    engine.set_crash_hook(crash_w2)
    with pytest.raises(RuntimeError, match="simulated_crash_w2"):
        engine.run_step_pair("task", tid, "analyse")

    with db.read_conn() as conn:
        steps_w2 = conn.execute(
            "SELECT step_id FROM workflow_steps WHERE entity_id = ?;", (tid,)
        ).fetchall()
    assert len(steps_w2) == 1
    assert str(steps_w2[0]["step_id"]) == first_step_id
    assert len(dispatch_calls) == 0

    # Window 3: Crash after effect succeeded, before completion txn
    def crash_w3(point: str) -> None:
        if point == "analyse:after_effect_before_completion":
            raise RuntimeError("simulated_crash_w3")

    engine.set_crash_hook(crash_w3)
    with pytest.raises(RuntimeError, match="simulated_crash_w3"):
        engine.run_step_pair("task", tid, "analyse")

    assert dispatch_calls == [first_step_id]

    # Startup step 5.1 resolves unfinished step whose execution succeeded WITHOUT re-running effect!
    resolved = engine.resolve_unfinished_steps()
    assert len(resolved) == 1
    assert resolved[0]["action"] == "completed_by_recovery"
    assert len(dispatch_calls) == 1  # Not called again!

    with db.read_conn() as conn:
        t_row = conn.execute("SELECT state, version FROM tasks WHERE id = ?;", (tid,)).fetchone()
        s_row = conn.execute(
            "SELECT status, transition_id FROM workflow_steps WHERE step_id = ?;",
            (first_step_id,),
        ).fetchone()
    assert t_row["state"] == TaskState.PLANNING.value
    assert s_row["status"] == "completed"
    assert s_row["transition_id"] is not None


def test_crash_window_4_completion_before_checkpoint_no_duplicate_transition(
    tmp_path: Path,
) -> None:
    """Window 4: completion txn commits at v+1, then crash before LangGraph writes checkpoint.
    Replaying X_run with the saved current_step_id sees status='completed' and writes NO second transition.
    """
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    with db.begin_immediate() as conn:
        _, tid, _, _ = _seed_project_task_plan_phase(conn)
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.REPOSITORY_ANALYSIS.value,
            reason="start",
            actor="user",
        )

    calls = 0

    def handler(
        conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str
    ) -> StepHandlerResult:
        nonlocal calls
        calls += 1
        return StepHandlerResult(
            to_state=TaskState.PLANNING.value,
            reason="Analysis done",
        )

    engine = LangGraphWorkflowEngine(db, step_handlers={"analyse": handler})

    def crash_w4(point: str) -> None:
        if point == "analyse:after_completion_before_checkpoint":
            raise RuntimeError("simulated_crash_w4")

    engine.set_crash_hook(crash_w4)
    with pytest.raises(RuntimeError, match="simulated_crash_w4"):
        engine.run_step_pair("task", tid, "analyse")

    assert calls == 1
    with db.read_conn() as conn:
        tr_count_before = conn.execute(
            "SELECT COUNT(*) FROM transitions WHERE entity_id = ?;", (tid,)
        ).fetchone()[0]
        step_row = conn.execute(
            "SELECT step_id, status FROM workflow_steps WHERE entity_id = ?;", (tid,)
        ).fetchone()
    assert step_row["status"] == "completed"

    # Now replay X_run directly with the pre-crash state holding current_step_id
    engine.set_crash_hook(None)
    replayed_state = engine.execute_step_run(
        {
            "entity_type": "task",
            "entity_id": tid,
            "project_id": "p",
            "state": TaskState.REPOSITORY_ANALYSIS.value,
            "entity_version": 1,
            "current_step_id": str(step_row["step_id"]),
            "generation": 1,
            "last_node": "analyse_begin",
            "retry_signal": None,
            "halted": False,
        },
        "analyse",
    )
    assert replayed_state["state"] == TaskState.PLANNING.value
    assert replayed_state["current_step_id"] is None
    assert calls == 1  # Handler NOT re-invoked

    with db.read_conn() as conn:
        tr_count_after = conn.execute(
            "SELECT COUNT(*) FROM transitions WHERE entity_id = ?;", (tid,)
        ).fetchone()[0]
    assert tr_count_after == tr_count_before  # No second transition!


def test_abandoned_step_routes_to_fresh_step_or_blocks(tmp_path: Path) -> None:
    """Verify ATLAS-L21: an abandoned step is never completed; if retry is permitted,
    X_run routes back to X_begin which allocates a fresh step_id.
    """
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    with db.begin_immediate() as conn:
        _, tid, _, _ = _seed_project_task_plan_phase(conn)
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.REPOSITORY_ANALYSIS.value,
            reason="start",
            actor="user",
        )

    seen_step_ids: list[str] = []

    def handler(
        conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str
    ) -> StepHandlerResult:
        seen_step_ids.append(step_id)
        return StepHandlerResult(
            to_state=TaskState.PLANNING.value,
            reason="Done on fresh step",
        )

    engine = LangGraphWorkflowEngine(db, step_handlers={"analyse": handler})

    # Allocate initial step via X_begin, then mark it interrupted and run step 5.1 so it becomes abandoned
    s0 = engine.execute_step_begin(
        {
            "entity_type": "task",
            "entity_id": tid,
            "project_id": "p",
            "state": TaskState.REPOSITORY_ANALYSIS.value,
            "entity_version": 1,
            "current_step_id": None,
            "generation": 1,
            "last_node": None,
            "retry_signal": None,
            "halted": False,
        },
        "analyse",
    )
    abandoned_step_id = str(s0["current_step_id"])
    engine.reserve_execution_dispatch(
        entity_type="task",
        entity_id=tid,
        step_id=abandoned_step_id,
        kind="agent",
        role="architect",
        model="claude-sonnet-4-5",
    )
    with db.begin_immediate() as conn:
        conn.execute(
            "UPDATE executions SET status = 'interrupted' WHERE step_id = ?;",
            (abandoned_step_id,),
        )
    engine.resolve_unfinished_steps()

    # Seed checkpoint holding the abandoned step_id and run the step pair:
    # X_run sees abandoned -> routes to X_begin -> allocates a NEW step_id -> completes!
    with db.read_conn() as conn:
        t_row = conn.execute("SELECT project_id FROM tasks WHERE id = ?;", (tid,)).fetchone()
    engine.seed_checkpoint(
        entity_type="task",
        entity_id=tid,
        project_id=str(t_row["project_id"]),
        state=TaskState.REPOSITORY_ANALYSIS.value,
        entity_version=1,
        generation=1,
        current_step_id=abandoned_step_id,
    )

    # First verify X_run on the abandoned step returns retry_signal='retry_begin' without calling handler
    s_abandoned_out = engine.execute_step_run(s0, "analyse")
    assert s_abandoned_out["retry_signal"] == "retry_begin"
    assert len(seen_step_ids) == 0

    # Now run full step pair -> allocates fresh step_id and completes
    final_state = engine.run_step_pair("task", tid, "analyse")
    assert final_state["state"] == TaskState.PLANNING.value
    assert len(seen_step_ids) == 1
    assert seen_step_ids[0] != abandoned_step_id


def test_pause_fence_overlay_completion_and_remote_merge_withdrawal(tmp_path: Path) -> None:
    """Verify ADR-006 §4 pause fence, overlay-aware completion (ATLAS-L03), and remote merge withdrawal."""
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    with db.begin_immediate() as conn:
        _, tid, _, phid = _seed_project_task_plan_phase(conn)
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.REPOSITORY_ANALYSIS.value,
            reason="start",
            actor="user",
        )

    engine = LangGraphWorkflowEngine(db)
    s0 = engine.execute_step_begin(
        {
            "entity_type": "task",
            "entity_id": tid,
            "project_id": "p",
            "state": TaskState.REPOSITORY_ANALYSIS.value,
            "entity_version": 1,
            "current_step_id": None,
            "generation": 1,
            "last_node": None,
            "retry_signal": None,
            "halted": False,
        },
        "analyse",
    )
    step_id = str(s0["current_step_id"])

    # User requests pause -> sets fence and transitions task to PAUSED(resume_to=REPOSITORY_ANALYSIS)
    pause_res = engine.request_pause("task", tid, actor="user")
    assert pause_res["state"] == "PAUSED"
    assert pause_res["resume_to"] == TaskState.REPOSITORY_ANALYSIS.value

    # Ordinary dispatch after fence is rejected!
    with pytest.raises(DispatchFencedError):
        engine.reserve_execution_dispatch(
            entity_type="task",
            entity_id=tid,
            step_id=step_id,
            effect_class="ordinary",
        )

    # Safety-class dispatch under fence is permitted!
    safety_exec_id = engine.reserve_execution_dispatch(
        entity_type="task",
        entity_id=tid,
        step_id=step_id,
        kind="gh",
        effect_class="safety",
    )
    assert safety_exec_id

    # Simulate an execution that finished just after user pause committed (overlay-aware completion, ATLAS-L03)
    with db.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE executions
            SET status = 'succeeded', exit_code = 0, result_json = ?
            WHERE id = ?;
            """,
            (
                json.dumps(
                    {
                        "to_state": TaskState.PLANNING.value,
                        "reason": "Completed while paused",
                    }
                ),
                safety_exec_id,
            ),
        )

    engine.resolve_unfinished_steps()
    with db.read_conn() as conn:
        t_after = conn.execute(
            "SELECT state, resume_to FROM tasks WHERE id = ?;", (tid,)
        ).fetchone()
        tr_underlying = conn.execute(
            "SELECT from_state, to_state, underlying FROM transitions WHERE entity_id = ? ORDER BY rowid DESC LIMIT 1;",
            (tid,),
        ).fetchone()
    # Entity remains PAUSED, while underlying resume_to advanced to PLANNING!
    assert t_after["state"] == "PAUSED"
    assert t_after["resume_to"] == TaskState.PLANNING.value
    assert tr_underlying["underlying"] == 1
    assert tr_underlying["from_state"] == TaskState.REPOSITORY_ANALYSIS.value
    assert tr_underlying["to_state"] == TaskState.PLANNING.value

    # Test remote merge withdrawal during phase pause in MERGE_PENDING_REMOTE (ATLAS-008 r4)
    with db.begin_immediate() as conn:
        conn.execute(
            "UPDATE phases SET state = 'MERGE_PENDING_REMOTE', version = 1 WHERE id = ?;",
            (phid,),
        )
    withdrawal_called = False

    def _withdraw(conn: sqlite3.Connection, p_id: str) -> bool:
        nonlocal withdrawal_called
        withdrawal_called = True
        return True

    p_pause = engine.request_pause("phase", phid, withdraw_remote_merge_fn=_withdraw)
    assert withdrawal_called is True
    assert p_pause["state"] == "PAUSED"
    assert p_pause["resume_to"] == "MERGE_PENDING_REMOTE"


def test_thread_classification_and_audited_rebuild(tmp_path: Path) -> None:
    """Verify ADR-006 §5.2 classification and §5.3 audited rebuild."""
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    with db.begin_immediate() as conn:
        pid, tid, _, _ = _seed_project_task_plan_phase(conn)
        _, v1 = apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.REPOSITORY_ANALYSIS.value,
            reason="start",
            actor="user",
        )

    engine = LangGraphWorkflowEngine(db)
    engine.seed_checkpoint(
        entity_type="task",
        entity_id=tid,
        project_id=pid,
        state=TaskState.REPOSITORY_ANALYSIS.value,
        entity_version=v1,
        generation=1,
    )

    # 1. Consistent when versions match
    cls1 = engine.classify_thread("task", tid)
    assert cls1.status == "consistent"

    # 2. Compatible when only user/recovery PAUSED overlay happened after checkpoint
    engine.request_pause("task", tid, actor="recovery")
    cls2 = engine.classify_thread("task", tid)
    assert cls2.status == "compatible"

    # 3. Incompatible when an uncheckpointed orchestrator transition happened out-of-band
    with db.begin_immediate() as conn:
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.REPOSITORY_ANALYSIS.value,
            reason="resume",
            actor="user",
        )
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.PLANNING.value,
            reason="uncheckpointed move",
            actor="orchestrator",
        )

    cls3 = engine.classify_thread("task", tid)
    assert cls3.status == "incompatible"
    assert cls3.rebuild_target == TaskState.PLANNING.value

    with db.read_conn() as conn:
        diverged_row = conn.execute(
            "SELECT state, blocked_reason, rebuild_target, generation FROM tasks WHERE id = ?;",
            (tid,),
        ).fetchone()
    assert diverged_row["state"] == "BLOCKED"
    assert diverged_row["blocked_reason"] == "engine_state_divergence"
    assert diverged_row["rebuild_target"] == TaskState.PLANNING.value

    # 4. Audited rebuild increments generation to 2 and restores consistent thread at PLANNING
    rebuilt = engine.rebuild_thread("task", tid, actor="user")
    assert rebuilt["generation"] == 2
    assert rebuilt["state"] == TaskState.PLANNING.value

    cls4 = engine.classify_thread("task", tid)
    assert cls4.status == "consistent"


def test_fail_closed_placeholder_and_zero_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ATLAS-017 fail-closed placeholder node and ADR-006 §1 zero network egress."""
    assert os.environ.get("LANGSMITH_TRACING") == "false"
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "false"

    # Trap any socket.connect attempt to prove zero outbound connections
    def _forbid_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"Outbound network connection attempted: {args}")

    monkeypatch.setattr(socket.socket, "connect", _forbid_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbid_connect)

    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    with db.begin_immediate() as conn:
        _, tid, _, _ = _seed_project_task_plan_phase(conn)

    # Default production engine has no mocked handlers -> raises CapabilityUnavailableError -> BLOCKED(capability_unavailable)
    engine = LangGraphWorkflowEngine(db)
    out = engine.start_task(tid)
    assert out["state"] == TaskState.BLOCKED.value

    with db.read_conn() as conn:
        t_row = conn.execute(
            "SELECT state, blocked_reason FROM tasks WHERE id = ?;", (tid,)
        ).fetchone()
    assert t_row["state"] == TaskState.BLOCKED.value
    assert t_row["blocked_reason"] == "capability_unavailable"
