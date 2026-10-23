"""Slice 1.4 tests: FSM transition tables, policy guards, Hypothesis stateful properties, and repo locks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atlas.db.connection import (
    DatabaseManager,
    insert_execution_checked,
    insert_review_round_checked,
)
from atlas.fsm.guards import (
    GuardContext,
    check_g_diff,
    check_g_merge_pre,
    check_g_merged,
    check_g_qa,
    check_g_rev,
    check_g_test,
)
from atlas.fsm.states import (
    PHASE_ALLOWED_TRANSITIONS,
    TASK_ALLOWED_TRANSITIONS,
    PhaseState,
    TaskState,
)
from atlas.fsm.transitions import (
    ConcurrentModificationError,
    InvalidTransitionError,
    RepoLockConflictError,
    acquire_repo_lock,
    apply_phase_transition,
    apply_task_transition,
    bump_project_policy_version,
    create_candidate,
    release_repo_lock,
)
from atlas.utils import new_ulid, utc_now_iso
from tests.test_db_integrity import _seed_project_task_plan_phase


def test_invalid_task_and_phase_transitions_rejected(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    with db.begin_immediate() as conn:
        _, tid, _, phid = _seed_project_task_plan_phase(conn)

    # Test sample disallowed task transitions
    for bad_target in (
        TaskState.PLAN_CHALLENGE.value,
        TaskState.READY_FOR_IMPLEMENTATION.value,
        TaskState.COMPLETED.value,
    ):
        assert (TaskState.CREATED.value, bad_target) not in TASK_ALLOWED_TRANSITIONS
        with pytest.raises(InvalidTransitionError), db.begin_immediate() as conn:
            apply_task_transition(
                conn,
                task_id=tid,
                to_state=bad_target,
                reason="invalid",
                actor="orchestrator",
            )

    # Test sample disallowed phase transitions from PENDING
    for bad_phase_target in (
        PhaseState.IMPLEMENTING.value,
        PhaseState.TESTING.value,
        PhaseState.MERGING.value,
        PhaseState.MERGED.value,
        PhaseState.COMPLETED.value,
    ):
        assert (PhaseState.PENDING.value, bad_phase_target) not in PHASE_ALLOWED_TRANSITIONS
        with pytest.raises(InvalidTransitionError), db.begin_immediate() as conn:
            apply_phase_transition(
                conn,
                phase_id=phid,
                to_state=bad_phase_target,
                reason="invalid",
                actor="orchestrator",
            )


def test_optimistic_concurrency_and_repo_lock_exclusivity(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    now = utc_now_iso()

    with db.begin_immediate() as conn:
        pid, tid, plid, ph1 = _seed_project_task_plan_phase(conn)
        ph2 = new_ulid()
        conn.execute(
            """
            INSERT INTO phases (
              id, project_id, task_id, plan_id, seq, kind, title, objective,
              spec_json, state, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 2, 'change', 'Phase 2', 'Obj', '{}', 'PENDING', 0, ?, ?);
            """,
            (ph2, pid, tid, plid, now, now),
        )

        # Acquire lock for ph1
        acquire_repo_lock(conn, project_id=pid, phase_id=ph1, holder_instance="inst-1")

        # Second phase ph2 on the same repository cannot acquire the lock
        with pytest.raises(RepoLockConflictError):
            acquire_repo_lock(conn, project_id=pid, phase_id=ph2, holder_instance="inst-1")

        # Move ph1 to READY -> PREPARING_WORKSPACE; cannot release lock while active
        apply_phase_transition(conn, phase_id=ph1, to_state=PhaseState.READY.value, reason="r", actor="orchestrator")
        apply_phase_transition(
            conn, phase_id=ph1, to_state=PhaseState.PREPARING_WORKSPACE.value, reason="r", actor="orchestrator"
        )
        with pytest.raises(RepoLockConflictError):
            release_repo_lock(conn, project_id=pid, phase_id=ph1)

        # Pause ph1 -> lock release is now permitted
        apply_phase_transition(conn, phase_id=ph1, to_state=PhaseState.PAUSED.value, reason="pause", actor="user")
        assert release_repo_lock(conn, project_id=pid, phase_id=ph1) is True

        # Stale expected_version raises ConcurrentModificationError
        with pytest.raises(ConcurrentModificationError):
            apply_phase_transition(
                conn,
                phase_id=ph1,
                to_state=PhaseState.PREPARING_WORKSPACE.value,
                reason="stale",
                actor="user",
                expected_version=0,
            )


def test_candidate_staleness_and_approval_superseding(tmp_path: Path) -> None:
    """Verify that evidence and approvals for candidate r1 cannot satisfy gates once candidate r2 is created."""
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    now = utc_now_iso()
    future = (datetime.now(UTC) + timedelta(hours=24)).isoformat()

    with db.begin_immediate() as conn:
        pid, tid, _, phid = _seed_project_task_plan_phase(conn)
        conn.execute(
            """
            UPDATE projects
            SET protection_status = 'known',
                protection_json = '{"required_checks": ["ci/test"]}'
            WHERE id = ?;
            """,
            (pid,),
        )
        conn.execute(
            """
            UPDATE phases
            SET spec_json = ?
            WHERE id = ?;
            """,
            (
                json.dumps(
                    {
                        "required_tests": ["pytest"],
                        "acceptance_criteria": [{"id": "AC-1", "text": "Works", "mandatory": True}],
                    }
                ),
                phid,
            ),
        )
        acquire_repo_lock(conn, project_id=pid, phase_id=phid, holder_instance="inst-1")

        # Create candidate r1
        cand_r1 = create_candidate(
            conn,
            phase_id=phid,
            commit_sha="sha-r1",
            tree_sha="tree-r1",
            parent_sha="base",
            author_agent="codex",
            author_provider="openai",
            author_family="openai",
            violations=[("protected_path", ".github/workflows/ci.yml", "Workflow modified")],
        )

        # Without protected_path approval, G-DIFF fails
        assert check_g_diff(conn, phid).ok is False

        # Add protected_path approval and merge approval for r1
        for kind in ("protected_path", "merge"):
            conn.execute(
                """
                INSERT INTO approvals (
                  id, project_id, phase_id, task_id, kind, candidate_id,
                  policy_version, status, requested_at, expires_at, decided_at, decided_by
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 'approved', ?, ?, ?, 'user');
                """,
                (new_ulid(), pid, phid, tid, kind, cand_r1, now, future, now),
            )
        assert check_g_diff(conn, phid).ok is True

        # Add executed test_run, independent code_review, and QA review for r1
        exec_r1 = new_ulid()
        insert_execution_checked(
            conn,
            {
                "id": exec_r1,
                "project_id": pid,
                "task_id": tid,
                "phase_id": phid,
                "candidate_id": cand_r1,
                "kind": "test",
                "status": "succeeded",
                "exit_code": 0,
                "argv_json": '["pytest"]',
                "correlation_id": new_ulid(),
                "created_at": now,
            },
        )
        conn.execute(
            """
            INSERT INTO test_runs (
              id, project_id, phase_id, candidate_id, execution_id,
              command, exit_code, passed, failed, skipped, evidence, integrity, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pytest', 0, 5, 0, 0, 'executed', 'verified', ?);
            """,
            (new_ulid(), pid, phid, cand_r1, exec_r1, now),
        )
        insert_review_round_checked(
            conn,
            {
                "id": new_ulid(),
                "project_id": pid,
                "subject_type": "candidate",
                "plan_id": None,
                "candidate_id": cand_r1,
                "kind": "code_review",
                "reviewer_agent": "claude",
                "author_agent": "codex",
                "reviewer_provider": "anthropic",
                "reviewer_family": "anthropic",
                "author_provider": "openai",
                "author_family": "openai",
                "verdict": "pass",
                "evidence": "agent_reported",
                "execution_id": None,
                "cycle": 1,
                "created_at": now,
            },
        )
        insert_review_round_checked(
            conn,
            {
                "id": new_ulid(),
                "project_id": pid,
                "subject_type": "candidate",
                "plan_id": None,
                "candidate_id": cand_r1,
                "kind": "qa",
                "reviewer_agent": "antigravity",
                "author_agent": "codex",
                "reviewer_provider": "google",
                "reviewer_family": "google",
                "author_provider": "openai",
                "author_family": "openai",
                "verdict": "pass",
                "evidence": "manual_imported",
                "criteria_json": json.dumps([{"id": "AC-1", "status": "met"}]),
                "source_integrity": "verified",
                "execution_id": None,
                "cycle": 1,
                "created_at": now,
            },
        )

        ctx_r1 = GuardContext(
            pr_head_sha="sha-r1",
            pr_exists=True,
            pr_mergeable="MERGEABLE",
            pr_review_decision_ok=True,
            ci_checks_status={"ci/test": "SUCCESS"},
        )
        assert check_g_test(conn, phid).ok is True
        assert check_g_rev(conn, phid).ok is True
        assert check_g_qa(conn, phid).ok is True
        assert check_g_merge_pre(conn, phid, ctx_r1).ok is True

        # Now create candidate r2 -> all r1 evidence and approvals become STALE!
        cand_r2 = create_candidate(
            conn,
            phase_id=phid,
            commit_sha="sha-r2",
            tree_sha="tree-r2",
            parent_sha="sha-r1",
            author_agent="codex",
            author_provider="openai",
            author_family="openai",
        )
        assert cand_r2 != cand_r1
        assert check_g_test(conn, phid).ok is False
        assert check_g_rev(conn, phid).ok is False
        assert check_g_qa(conn, phid).ok is False
        assert check_g_merge_pre(conn, phid, ctx_r1).ok is False

        # Also verify policy_version bump supersedes open merge approvals
        conn.execute(
            """
            INSERT INTO approvals (
              id, project_id, phase_id, task_id, kind, candidate_id,
              policy_version, status, requested_at, expires_at, decided_at, decided_by
            ) VALUES (?, ?, ?, ?, 'merge', ?, 1, 'approved', ?, ?, ?, 'user');
            """,
            (new_ulid(), pid, phid, tid, cand_r2, now, future, now),
        )
        bump_project_policy_version(conn, pid)
        superseded = conn.execute(
            "SELECT status FROM approvals WHERE candidate_id = ? AND kind = 'merge';",
            (cand_r2,),
        ).fetchone()
        assert superseded["status"] == "superseded"


@settings(max_examples=40, deadline=None)
@given(
    has_test=st.booleans(),
    test_same_cand=st.booleans(),
    has_rev=st.booleans(),
    rev_same_cand=st.booleans(),
    has_qa=st.booleans(),
    qa_same_cand=st.booleans(),
    has_ci=st.booleans(),
    ci_same_cand=st.booleans(),
    has_approval=st.booleans(),
    approval_same_cand=st.booleans(),
    merged_head_same_cand=st.booleans(),
)
def test_hypothesis_no_merged_without_same_candidate_evidence(
    tmp_path_factory: pytest.TempPathFactory,
    has_test: bool,
    test_same_cand: bool,
    has_rev: bool,
    rev_same_cand: bool,
    has_qa: bool,
    qa_same_cand: bool,
    has_ci: bool,
    ci_same_cand: bool,
    has_approval: bool,
    approval_same_cand: bool,
    merged_head_same_cand: bool,
) -> None:
    """Property test (workflow-state-machine.md §6):
    G-MERGE-PRE and G-MERGED pass iff EVERY piece of required evidence and the merged head
    match the phase's current candidate.
    """
    base_dir = tmp_path_factory.mktemp("hyp-fsm")
    db = DatabaseManager(base_dir)
    db.initialize()
    now = utc_now_iso()
    future = (datetime.now(UTC) + timedelta(hours=12)).isoformat()

    with db.begin_immediate() as conn:
        pid, tid, _, phid = _seed_project_task_plan_phase(conn)
        conn.execute(
            """
            UPDATE projects
            SET protection_status = 'known',
                protection_json = '{"required_checks": ["ci/build"]}'
            WHERE id = ?;
            """,
            (pid,),
        )
        conn.execute(
            """
            UPDATE phases
            SET spec_json = ?
            WHERE id = ?;
            """,
            (
                json.dumps(
                    {
                        "required_tests": ["pytest"],
                        "acceptance_criteria": [{"id": "AC-1", "text": "Crit", "mandatory": True}],
                    }
                ),
                phid,
            ),
        )
        acquire_repo_lock(conn, project_id=pid, phase_id=phid, holder_instance="hyp")

        cand_old = create_candidate(
            conn,
            phase_id=phid,
            commit_sha="sha-old",
            tree_sha="tree-old",
            parent_sha="base",
            author_agent="codex",
        )
        cand_cur = create_candidate(
            conn,
            phase_id=phid,
            commit_sha="sha-cur",
            tree_sha="tree-cur",
            parent_sha="sha-old",
            author_agent="codex",
        )

        if has_test:
            target_c = cand_cur if test_same_cand else cand_old
            ex_id = new_ulid()
            insert_execution_checked(
                conn,
                {
                    "id": ex_id,
                    "project_id": pid,
                    "task_id": tid,
                    "phase_id": phid,
                    "candidate_id": target_c,
                    "kind": "test",
                    "status": "succeeded",
                    "exit_code": 0,
                    "argv_json": "[]",
                    "correlation_id": new_ulid(),
                    "created_at": now,
                },
            )
            conn.execute(
                """
                INSERT INTO test_runs (
                  id, project_id, phase_id, candidate_id, execution_id,
                  command, exit_code, evidence, integrity, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pytest', 0, 'executed', 'verified', ?);
                """,
                (new_ulid(), pid, phid, target_c, ex_id, now),
            )

        if has_rev:
            target_c = cand_cur if rev_same_cand else cand_old
            insert_review_round_checked(
                conn,
                {
                    "id": new_ulid(),
                    "project_id": pid,
                    "subject_type": "candidate",
                    "plan_id": None,
                    "candidate_id": target_c,
                    "kind": "code_review",
                    "reviewer_agent": "claude",
                    "author_agent": "codex",
                    "reviewer_provider": "anthropic",
                    "reviewer_family": "anthropic",
                    "author_provider": "openai",
                    "author_family": "openai",
                    "verdict": "pass",
                    "evidence": "agent_reported",
                    "execution_id": None,
                    "cycle": 1,
                    "created_at": now,
                },
            )

        if has_qa:
            target_c = cand_cur if qa_same_cand else cand_old
            insert_review_round_checked(
                conn,
                {
                    "id": new_ulid(),
                    "project_id": pid,
                    "subject_type": "candidate",
                    "plan_id": None,
                    "candidate_id": target_c,
                    "kind": "qa",
                    "reviewer_agent": "antigravity",
                    "author_agent": "codex",
                    "reviewer_provider": "google",
                    "reviewer_family": "google",
                    "author_provider": "openai",
                    "author_family": "openai",
                    "verdict": "pass",
                    "evidence": "manual_imported",
                    "criteria_json": json.dumps([{"id": "AC-1", "status": "met"}]),
                    "source_integrity": "verified",
                    "execution_id": None,
                    "cycle": 1,
                    "created_at": now,
                },
            )

        if has_approval:
            target_c = cand_cur if approval_same_cand else cand_old
            conn.execute(
                """
                INSERT INTO approvals (
                  id, project_id, phase_id, task_id, kind, candidate_id,
                  policy_version, status, requested_at, expires_at, decided_at, decided_by
                ) VALUES (?, ?, ?, ?, 'merge', ?, 1, 'approved', ?, ?, ?, 'user');
                """,
                (new_ulid(), pid, phid, tid, target_c, now, future, now),
            )

        ci_sha = "sha-cur" if ci_same_cand else "sha-old"
        ctx = GuardContext(
            pr_head_sha=ci_sha,
            pr_exists=True,
            pr_mergeable="MERGEABLE",
            pr_review_decision_ok=True,
            ci_checks_status={"ci/build": "SUCCESS" if has_ci else "FAILURE"},
            pr_state="MERGED",
            merged_base_branch="main",
            merge_commit_oid="merged-oid",
            merge_commit_reachable_on_base=True,
        )

        merge_pre = check_g_merge_pre(conn, phid, ctx)
        expected_pre_ok = (
            has_test
            and test_same_cand
            and has_rev
            and rev_same_cand
            and has_qa
            and qa_same_cand
            and has_ci
            and ci_same_cand
            and has_approval
            and approval_same_cand
        )
        assert merge_pre.ok is expected_pre_ok

        merge_ctx = GuardContext(
            pr_state="MERGED",
            pr_head_sha="sha-cur" if merged_head_same_cand else "sha-old",
            merged_base_branch="main",
            merge_commit_oid="merged-oid",
            merge_commit_reachable_on_base=True,
        )
        g_merged = check_g_merged(conn, phid, merge_ctx)
        assert g_merged.ok is merged_head_same_cand
        if not merged_head_same_cand:
            assert g_merged.blocked_reason == "external_merge_mismatch"
