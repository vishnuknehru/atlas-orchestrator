"""Transactional FSM transition application, candidate creation, and repo locks (docs/workflow-state-machine.md §3)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from atlas.fsm.guards import GuardResult
from atlas.fsm.states import (
    PHASE_CHECKPOINT_STATES,
    TASK_CHECKPOINT_STATES,
    PhaseState,
    TaskState,
    is_phase_transition_allowed,
    is_task_transition_allowed,
)
from atlas.utils import new_ulid, utc_now_iso


class EntityNotFoundError(ValueError):
    """Raised when the task, phase or project being acted on does not exist."""


class InvalidTransitionError(ValueError):
    """Raised when a transition (from_state, to_state) is not in ALLOWED."""


class GuardFailedError(ValueError):
    """Raised when a policy engine guard returns False."""


class ConcurrentModificationError(RuntimeError):
    """Raised when optimistic concurrency version check fails."""


class RepoLockConflictError(RuntimeError):
    """Raised when another active phase already holds the repository lock."""


def apply_task_transition(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    to_state: str,
    reason: str,
    actor: str,
    expected_version: int | None = None,
    blocked_reason: str | None = None,
    evidence_refs: list[str] | None = None,
    correlation_id: str | None = None,
    underlying: bool = False,
    guard_fn: Callable[[sqlite3.Connection], GuardResult] | None = None,
) -> tuple[str, int]:
    """Apply a validated Task FSM transition inside an active transaction.

    Supports overlay-aware underlying transitions (ADR-006 §3) when underlying=True
    and the task is currently in PAUSED or BLOCKED with a recorded resume_to.
    """
    row = conn.execute(
        """
        SELECT t.id, t.project_id, t.state, t.resume_to, t.blocked_reason, t.version, pr.policy_version
        FROM tasks t
        JOIN projects pr ON pr.id = t.project_id
        WHERE t.id = ?;
        """,
        (task_id,),
    ).fetchone()
    if not row:
        raise EntityNotFoundError(f"Task {task_id!r} not found")

    current_state = str(row["state"])
    current_version = int(row["version"])
    resume_to = row["resume_to"]
    project_id = str(row["project_id"])
    policy_version = int(row["policy_version"])

    if expected_version is not None and current_version != expected_version:
        raise ConcurrentModificationError(
            f"Task {task_id} version mismatch: expected {expected_version}, got {current_version}"
        )

    if underlying and current_state in (TaskState.PAUSED.value, TaskState.BLOCKED.value) and resume_to:
        source_state = str(resume_to)
        if not is_task_transition_allowed(source_state, to_state):
            raise InvalidTransitionError(
                f"Underlying task transition ({source_state!r} -> {to_state!r}) not allowed"
            )
    else:
        underlying = False
        source_state = current_state
        if not is_task_transition_allowed(source_state, to_state):
            raise InvalidTransitionError(
                f"Task transition ({source_state!r} -> {to_state!r}) not allowed"
            )
        if source_state == TaskState.PAUSED.value and resume_to and to_state not in (
            TaskState.CANCELLED.value,
            TaskState.FAILED.value,
            TaskState.BLOCKED.value,
        ):
            if to_state != str(resume_to):
                raise InvalidTransitionError(
                    f"Paused task must resume to its recorded resume_to={resume_to!r}, not {to_state!r}"
                )

    refs = list(evidence_refs or [])
    if guard_fn is not None:
        g_res = guard_fn(conn)
        if not g_res.ok:
            raise GuardFailedError(f"{g_res.guard_id} failed: {g_res.reason}")
        for r in g_res.evidence_refs:
            if r not in refs:
                refs.append(r)

    now = utc_now_iso()
    corr_id = correlation_id or new_ulid()
    new_version = current_version + 1

    if underlying:
        new_state = current_state
        new_resume_to = to_state
        # The overlay stays in place, so keep its reason unless the caller supplies a new one
        new_blocked_reason = blocked_reason or row["blocked_reason"]
    else:
        new_state = to_state
        if to_state == TaskState.PAUSED.value:
            new_resume_to = source_state
            new_blocked_reason = blocked_reason
        elif to_state == TaskState.BLOCKED.value:
            new_resume_to = source_state
            new_blocked_reason = blocked_reason or reason
        else:
            new_resume_to = None
            new_blocked_reason = None

    cur = conn.execute(
        """
        UPDATE tasks
        SET state = ?,
            resume_to = ?,
            blocked_reason = ?,
            version = ?,
            updated_at = ?
        WHERE id = ? AND version = ?;
        """,
        (
            new_state,
            new_resume_to,
            new_blocked_reason,
            new_version,
            now,
            task_id,
            current_version,
        ),
    )
    if cur.rowcount != 1:
        raise ConcurrentModificationError(f"Concurrent update on task {task_id}")

    transition_id = new_ulid()
    conn.execute(
        """
        INSERT INTO transitions (
          id, project_id, entity_type, entity_id, from_state, to_state,
          reason, actor, candidate_id, policy_version, underlying,
          evidence_refs_json, correlation_id, at
        ) VALUES (?, ?, 'task', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?);
        """,
        (
            transition_id,
            project_id,
            task_id,
            source_state,
            to_state,
            reason,
            actor,
            policy_version,
            1 if underlying else 0,
            json.dumps(refs),
            corr_id,
            now,
        ),
    )

    if to_state in TASK_CHECKPOINT_STATES:
        conn.execute(
            """
            INSERT INTO checkpoints (id, project_id, task_id, phase_id, state, snapshot_json, created_at)
            VALUES (?, ?, ?, NULL, ?, ?, ?);
            """,
            (
                new_ulid(),
                project_id,
                task_id,
                new_state,
                json.dumps(
                    {
                        "state": new_state,
                        "resume_to": new_resume_to,
                        "version": new_version,
                        "transition_id": transition_id,
                    }
                ),
                now,
            ),
        )

    return transition_id, new_version


def apply_phase_transition(
    conn: sqlite3.Connection,
    *,
    phase_id: str,
    to_state: str,
    reason: str,
    actor: str,
    expected_version: int | None = None,
    blocked_reason: str | None = None,
    evidence_refs: list[str] | None = None,
    correlation_id: str | None = None,
    underlying: bool = False,
    guard_fn: Callable[[sqlite3.Connection], GuardResult] | None = None,
) -> tuple[str, int]:
    """Apply a validated Phase FSM transition inside an active transaction."""
    row = conn.execute(
        """
        SELECT p.id, p.project_id, p.task_id, p.state, p.resume_to, p.blocked_reason, p.version,
               p.current_candidate_id, p.remote_merge_withdrawn, pr.policy_version
        FROM phases p
        JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?;
        """,
        (phase_id,),
    ).fetchone()
    if not row:
        raise EntityNotFoundError(f"Phase {phase_id!r} not found")

    current_state = str(row["state"])
    current_version = int(row["version"])
    resume_to = row["resume_to"]
    project_id = str(row["project_id"])
    task_id = str(row["task_id"])
    candidate_id = row["current_candidate_id"]
    policy_version = int(row["policy_version"])
    remote_merge_withdrawn = int(row["remote_merge_withdrawn"] or 0)

    if expected_version is not None and current_version != expected_version:
        raise ConcurrentModificationError(
            f"Phase {phase_id} version mismatch: expected {expected_version}, got {current_version}"
        )

    if underlying and current_state in (PhaseState.PAUSED.value, PhaseState.BLOCKED.value) and resume_to:
        source_state = str(resume_to)
        if not is_phase_transition_allowed(source_state, to_state):
            raise InvalidTransitionError(
                f"Underlying phase transition ({source_state!r} -> {to_state!r}) not allowed"
            )
    else:
        underlying = False
        source_state = current_state
        if not is_phase_transition_allowed(source_state, to_state):
            raise InvalidTransitionError(
                f"Phase transition ({source_state!r} -> {to_state!r}) not allowed"
            )
        # Enforce ATLAS-008 r4: pausing from MERGE_PENDING_REMOTE or CANCELLING_REMOTE_MERGE
        # requires remote_merge_withdrawn to be confirmed first
        if (
            to_state == PhaseState.PAUSED.value
            and source_state
            in (
                PhaseState.MERGE_PENDING_REMOTE.value,
                PhaseState.CANCELLING_REMOTE_MERGE.value,
            )
            and not remote_merge_withdrawn
        ):
            raise GuardFailedError(
                "Cannot enter PAUSED from MERGE_PENDING_REMOTE/CANCELLING_REMOTE_MERGE before remote merge withdrawal is confirmed (ATLAS-008 r4)"
            )
        if source_state == PhaseState.PAUSED.value and resume_to and to_state not in (
            PhaseState.CANCELLED.value,
            PhaseState.FAILED.value,
            PhaseState.BLOCKED.value,
        ):
            if to_state != str(resume_to):
                raise InvalidTransitionError(
                    f"Paused phase must resume to its recorded resume_to={resume_to!r}, not {to_state!r}"
                )

    refs = list(evidence_refs or [])
    if guard_fn is not None:
        g_res = guard_fn(conn)
        if not g_res.ok:
            raise GuardFailedError(f"{g_res.guard_id} failed: {g_res.reason}")
        for r in g_res.evidence_refs:
            if r not in refs:
                refs.append(r)

    now = utc_now_iso()
    corr_id = correlation_id or new_ulid()
    new_version = current_version + 1

    if underlying:
        new_state = current_state
        new_resume_to = to_state
        # The overlay stays in place, so keep its reason unless the caller supplies a new one
        new_blocked_reason = blocked_reason or row["blocked_reason"]
    else:
        new_state = to_state
        if to_state == PhaseState.PAUSED.value:
            new_resume_to = source_state
            new_blocked_reason = blocked_reason
        elif to_state == PhaseState.BLOCKED.value:
            new_resume_to = source_state
            new_blocked_reason = blocked_reason or reason
        else:
            new_resume_to = None
            new_blocked_reason = None

    cur = conn.execute(
        """
        UPDATE phases
        SET state = ?,
            resume_to = ?,
            blocked_reason = ?,
            version = ?,
            updated_at = ?
        WHERE id = ? AND version = ?;
        """,
        (
            new_state,
            new_resume_to,
            new_blocked_reason,
            new_version,
            now,
            phase_id,
            current_version,
        ),
    )
    if cur.rowcount != 1:
        raise ConcurrentModificationError(f"Concurrent update on phase {phase_id}")

    transition_id = new_ulid()
    conn.execute(
        """
        INSERT INTO transitions (
          id, project_id, entity_type, entity_id, from_state, to_state,
          reason, actor, candidate_id, policy_version, underlying,
          evidence_refs_json, correlation_id, at
        ) VALUES (?, ?, 'phase', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            transition_id,
            project_id,
            phase_id,
            source_state,
            to_state,
            reason,
            actor,
            candidate_id,
            policy_version,
            1 if underlying else 0,
            json.dumps(refs),
            corr_id,
            now,
        ),
    )

    if to_state in PHASE_CHECKPOINT_STATES:
        conn.execute(
            """
            INSERT INTO checkpoints (id, project_id, task_id, phase_id, state, snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                new_ulid(),
                project_id,
                task_id,
                phase_id,
                new_state,
                json.dumps(
                    {
                        "state": new_state,
                        "resume_to": new_resume_to,
                        "candidate_id": candidate_id,
                        "version": new_version,
                        "transition_id": transition_id,
                    }
                ),
                now,
            ),
        )

    return transition_id, new_version


def create_candidate(
    conn: sqlite3.Connection,
    *,
    phase_id: str,
    commit_sha: str,
    tree_sha: str,
    parent_sha: str,
    author_agent: str,
    author_model: str | None = None,
    author_provider: str = "openai",
    author_family: str = "openai",
    author_agent_revision: int = 1,
    created_by_execution: str | None = None,
    violations: list[tuple[str, str | None, str]] | None = None,
) -> str:
    """Create a new candidate revision n+1, update phase.current_candidate_id, and supersede older approvals."""
    phase = conn.execute(
        "SELECT id, project_id, current_candidate_id FROM phases WHERE id = ?;",
        (phase_id,),
    ).fetchone()
    if not phase:
        raise EntityNotFoundError(f"Phase {phase_id!r} not found")

    project_id = str(phase["project_id"])
    max_rev_row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) AS max_rev FROM candidates WHERE phase_id = ?;",
        (phase_id,),
    ).fetchone()
    next_rev = int(max_rev_row["max_rev"]) + 1

    violation_list = list(violations or [])
    guard_status = "held" if violation_list else "clear"
    candidate_id = new_ulid()
    now = utc_now_iso()

    conn.execute(
        """
        INSERT INTO candidates (
          id, project_id, phase_id, revision, commit_sha, tree_sha, parent_sha,
          author_agent, author_model, author_provider, author_family, author_agent_revision,
          created_by_execution, guard_status, guard_report_ref, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?);
        """,
        (
            candidate_id,
            project_id,
            phase_id,
            next_rev,
            commit_sha,
            tree_sha,
            parent_sha,
            author_agent,
            author_model,
            author_provider,
            author_family,
            author_agent_revision,
            created_by_execution,
            guard_status,
            now,
        ),
    )

    for v_kind, v_path, v_detail in violation_list:
        conn.execute(
            """
            INSERT INTO candidate_guard_violations (id, project_id, candidate_id, kind, path, detail)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (new_ulid(), project_id, candidate_id, v_kind, v_path, v_detail),
        )

    # Mark open/approved approvals for older candidates on this phase as superseded (database-design.md §4)
    conn.execute(
        """
        UPDATE approvals
        SET status = 'superseded'
        WHERE project_id = ?
          AND phase_id = ?
          AND candidate_id IS NOT NULL
          AND candidate_id <> ?
          AND status IN ('pending', 'approved');
        """,
        (project_id, phase_id, candidate_id),
    )

    conn.execute(
        "UPDATE phases SET current_candidate_id = ?, updated_at = ? WHERE id = ?;",
        (candidate_id, now, phase_id),
    )
    return candidate_id


def bump_project_policy_version(conn: sqlite3.Connection, project_id: str) -> int:
    """Increment projects.policy_version and mark open/approved merge approvals superseded."""
    row = conn.execute(
        "SELECT policy_version FROM projects WHERE id = ?;", (project_id,)
    ).fetchone()
    if not row:
        raise EntityNotFoundError(f"Project {project_id!r} not found")
    new_ver = int(row["policy_version"]) + 1
    now = utc_now_iso()
    conn.execute(
        "UPDATE projects SET policy_version = ?, updated_at = ? WHERE id = ?;",
        (new_ver, now, project_id),
    )
    conn.execute(
        """
        UPDATE approvals
        SET status = 'superseded'
        WHERE project_id = ?
          AND kind = 'merge'
          AND status IN ('pending', 'approved');
        """,
        (project_id,),
    )
    return new_ver


def acquire_repo_lock(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    phase_id: str,
    holder_instance: str,
) -> None:
    """Acquire the per-repository lock for an active code-changing phase."""
    existing = conn.execute(
        "SELECT phase_id, holder_instance FROM repo_locks WHERE project_id = ?;",
        (project_id,),
    ).fetchone()
    if existing:
        if existing["phase_id"] == phase_id:
            return
        raise RepoLockConflictError(
            f"Repository lock for project {project_id!r} is already held by phase {existing['phase_id']!r}"
        )

    conn.execute(
        """
        INSERT INTO repo_locks (project_id, phase_id, acquired_at, holder_instance)
        VALUES (?, ?, ?, ?);
        """,
        (project_id, phase_id, utc_now_iso(), holder_instance),
    )


def release_repo_lock(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    phase_id: str | None = None,
) -> bool:
    """Release the repository lock only when the holding phase is PAUSED, BLOCKED, or terminal."""
    existing = conn.execute(
        """
        SELECT rl.phase_id, p.state, p.remote_merge_withdrawn
        FROM repo_locks rl
        JOIN phases p ON p.id = rl.phase_id AND p.project_id = rl.project_id
        WHERE rl.project_id = ?;
        """,
        (project_id,),
    ).fetchone()
    if not existing:
        return False

    if phase_id is not None and existing["phase_id"] != phase_id:
        return False

    state = str(existing["state"])
    allowed_release_states = {
        PhaseState.PAUSED.value,
        PhaseState.BLOCKED.value,
        PhaseState.COMPLETED.value,
        PhaseState.ROLLED_BACK.value,
        PhaseState.CANCELLED.value,
        PhaseState.FAILED.value,
    }
    if state not in allowed_release_states:
        raise RepoLockConflictError(
            f"Cannot release repo_lock while phase {existing['phase_id']} is in active state {state}"
        )

    conn.execute("DELETE FROM repo_locks WHERE project_id = ?;", (project_id,))
    return True
