"""Task and Phase Finite State Machine definitions and transition tables (docs/workflow-state-machine.md)."""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    CREATED = "CREATED"
    REPOSITORY_ANALYSIS = "REPOSITORY_ANALYSIS"
    PLANNING = "PLANNING"
    PLAN_CHALLENGE = "PLAN_CHALLENGE"
    PLAN_REVISION = "PLAN_REVISION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY_FOR_IMPLEMENTATION = "READY_FOR_IMPLEMENTATION"
    EXECUTING_PHASES = "EXECUTING_PHASES"
    COMPLETED = "COMPLETED"
    # Overlay / fault / terminal states
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class PhaseState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    PREPARING_WORKSPACE = "PREPARING_WORKSPACE"
    IMPLEMENTING = "IMPLEMENTING"
    CANDIDATE_CREATED = "CANDIDATE_CREATED"
    TESTING = "TESTING"
    CODE_REVIEW = "CODE_REVIEW"
    QA_REVIEW = "QA_REVIEW"
    AWAITING_MANUAL = "AWAITING_MANUAL"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    FIXING = "FIXING"
    READY_FOR_DELIVERY = "READY_FOR_DELIVERY"
    PUSHED = "PUSHED"
    PR_OPEN = "PR_OPEN"
    CI_VALIDATION = "CI_VALIDATION"
    MERGING = "MERGING"
    MERGE_PENDING_REMOTE = "MERGE_PENDING_REMOTE"
    CANCELLING_REMOTE_MERGE = "CANCELLING_REMOTE_MERGE"
    MERGED = "MERGED"
    POST_MERGE_VERIFICATION = "POST_MERGE_VERIFICATION"
    ROLLBACK_PROPOSED = "ROLLBACK_PROPOSED"
    ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
    ROLLED_BACK = "ROLLED_BACK"
    COMPLETED = "COMPLETED"
    # Overlay / fault / terminal states
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


TASK_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.CANCELLED, TaskState.FAILED}
)

PHASE_TERMINAL_STATES: frozenset[PhaseState] = frozenset(
    {
        PhaseState.COMPLETED,
        PhaseState.ROLLED_BACK,
        PhaseState.CANCELLED,
        PhaseState.FAILED,
    }
)

TASK_NON_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    s for s in TaskState if s not in TASK_TERMINAL_STATES
)

PHASE_NON_TERMINAL_STATES: frozenset[PhaseState] = frozenset(
    s for s in PhaseState if s not in PHASE_TERMINAL_STATES
)

# Direct workflow transitions for Task FSM (workflow-state-machine.md §1)
_TASK_BASE_EDGES: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.REPOSITORY_ANALYSIS}),
    TaskState.REPOSITORY_ANALYSIS: frozenset({TaskState.PLANNING}),
    TaskState.PLANNING: frozenset({TaskState.PLAN_CHALLENGE}),
    TaskState.PLAN_CHALLENGE: frozenset(
        {TaskState.PLAN_REVISION, TaskState.AWAITING_APPROVAL}
    ),
    TaskState.PLAN_REVISION: frozenset(
        {TaskState.PLAN_CHALLENGE, TaskState.AWAITING_APPROVAL}
    ),
    TaskState.AWAITING_APPROVAL: frozenset(
        {TaskState.READY_FOR_IMPLEMENTATION, TaskState.PLAN_REVISION}
    ),
    TaskState.READY_FOR_IMPLEMENTATION: frozenset({TaskState.EXECUTING_PHASES}),
    TaskState.EXECUTING_PHASES: frozenset({TaskState.COMPLETED}),
    TaskState.BLOCKED: frozenset(
        {
            TaskState.CREATED,
            TaskState.REPOSITORY_ANALYSIS,
            TaskState.PLANNING,
            TaskState.PLAN_CHALLENGE,
            TaskState.PLAN_REVISION,
            TaskState.READY_FOR_IMPLEMENTATION,
            TaskState.EXECUTING_PHASES,
        }
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.FAILED: frozenset(),
}

# Build full TASK_ALLOWED_TRANSITIONS including overlay entries (PAUSED, BLOCKED, CANCELLED, FAILED)
TASK_ALLOWED_TRANSITIONS: set[tuple[str, str]] = set()
for src, targets in _TASK_BASE_EDGES.items():
    for dst in targets:
        TASK_ALLOWED_TRANSITIONS.add((src.value, dst.value))

for src in TASK_NON_TERMINAL_STATES:
    for overlay in (
        TaskState.PAUSED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    ):
        if src != overlay:
            TASK_ALLOWED_TRANSITIONS.add((src.value, overlay.value))
    # PAUSED can resume to any non-terminal underlying state
    if src != TaskState.PAUSED:
        TASK_ALLOWED_TRANSITIONS.add((TaskState.PAUSED.value, src.value))


# Direct workflow transitions for Phase FSM (workflow-state-machine.md §2)
_PHASE_BASE_EDGES: dict[PhaseState, frozenset[PhaseState]] = {
    PhaseState.PENDING: frozenset({PhaseState.READY}),
    PhaseState.READY: frozenset({PhaseState.PREPARING_WORKSPACE}),
    PhaseState.PREPARING_WORKSPACE: frozenset({PhaseState.IMPLEMENTING}),
    PhaseState.IMPLEMENTING: frozenset({PhaseState.CANDIDATE_CREATED}),
    PhaseState.CANDIDATE_CREATED: frozenset(
        {PhaseState.TESTING, PhaseState.AWAITING_APPROVAL}
    ),
    PhaseState.AWAITING_APPROVAL: frozenset(
        {
            PhaseState.TESTING,
            PhaseState.MERGING,
            PhaseState.CI_VALIDATION,
            PhaseState.FIXING,
        }
    ),
    PhaseState.TESTING: frozenset({PhaseState.CODE_REVIEW, PhaseState.FIXING}),
    PhaseState.CODE_REVIEW: frozenset({PhaseState.QA_REVIEW, PhaseState.FIXING}),
    PhaseState.QA_REVIEW: frozenset(
        {
            PhaseState.AWAITING_MANUAL,
            PhaseState.FIXING,
            PhaseState.READY_FOR_DELIVERY,
        }
    ),
    PhaseState.AWAITING_MANUAL: frozenset({PhaseState.QA_REVIEW}),
    PhaseState.FIXING: frozenset({PhaseState.CANDIDATE_CREATED}),
    PhaseState.READY_FOR_DELIVERY: frozenset({PhaseState.PUSHED}),
    PhaseState.PUSHED: frozenset({PhaseState.PR_OPEN}),
    PhaseState.PR_OPEN: frozenset({PhaseState.CI_VALIDATION}),
    PhaseState.CI_VALIDATION: frozenset(
        {
            PhaseState.FIXING,
            PhaseState.CANDIDATE_CREATED,
            PhaseState.AWAITING_APPROVAL,
        }
    ),
    PhaseState.MERGING: frozenset(
        {PhaseState.MERGE_PENDING_REMOTE, PhaseState.MERGED}
    ),
    PhaseState.MERGE_PENDING_REMOTE: frozenset(
        {
            PhaseState.MERGED,
            PhaseState.CI_VALIDATION,
            PhaseState.CANCELLING_REMOTE_MERGE,
        }
    ),
    PhaseState.CANCELLING_REMOTE_MERGE: frozenset(
        {PhaseState.CI_VALIDATION, PhaseState.MERGED}
    ),
    PhaseState.MERGED: frozenset({PhaseState.POST_MERGE_VERIFICATION}),
    PhaseState.POST_MERGE_VERIFICATION: frozenset(
        {PhaseState.COMPLETED, PhaseState.ROLLBACK_PROPOSED}
    ),
    PhaseState.ROLLBACK_PROPOSED: frozenset(
        {PhaseState.COMPLETED, PhaseState.ROLLBACK_IN_PROGRESS}
    ),
    PhaseState.ROLLBACK_IN_PROGRESS: frozenset({PhaseState.ROLLED_BACK}),
    PhaseState.BLOCKED: frozenset(
        {
            PhaseState.PENDING,
            PhaseState.READY,
            PhaseState.PREPARING_WORKSPACE,
            PhaseState.IMPLEMENTING,
            PhaseState.CANDIDATE_CREATED,
            PhaseState.TESTING,
            PhaseState.CODE_REVIEW,
            PhaseState.QA_REVIEW,
            PhaseState.FIXING,
            PhaseState.READY_FOR_DELIVERY,
            PhaseState.PUSHED,
            PhaseState.PR_OPEN,
            PhaseState.CI_VALIDATION,
        }
    ),
    PhaseState.COMPLETED: frozenset(),
    PhaseState.ROLLED_BACK: frozenset(),
    PhaseState.CANCELLED: frozenset(),
    PhaseState.FAILED: frozenset(),
}

PHASE_ALLOWED_TRANSITIONS: set[tuple[str, str]] = set()
for p_src, p_targets in _PHASE_BASE_EDGES.items():
    for p_dst in p_targets:
        PHASE_ALLOWED_TRANSITIONS.add((p_src.value, p_dst.value))

for p_src in PHASE_NON_TERMINAL_STATES:
    for p_overlay in (
        PhaseState.PAUSED,
        PhaseState.BLOCKED,
        PhaseState.CANCELLED,
        PhaseState.FAILED,
    ):
        if p_src != p_overlay:
            PHASE_ALLOWED_TRANSITIONS.add((p_src.value, p_overlay.value))
    # PAUSED can resume to any non-terminal state EXCEPT MERGING directly from remote-merge withdrawal
    if p_src != PhaseState.PAUSED:
        PHASE_ALLOWED_TRANSITIONS.add((PhaseState.PAUSED.value, p_src.value))


# Checkpoint states (workflow-state-machine.md §3.3)
TASK_CHECKPOINT_STATES: frozenset[str] = frozenset(
    {
        TaskState.AWAITING_APPROVAL.value,
        TaskState.READY_FOR_IMPLEMENTATION.value,
        TaskState.EXECUTING_PHASES.value,
        TaskState.COMPLETED.value,
        TaskState.PAUSED.value,
        TaskState.BLOCKED.value,
        TaskState.CANCELLED.value,
        TaskState.FAILED.value,
    }
)

PHASE_CHECKPOINT_STATES: frozenset[str] = frozenset(
    {
        PhaseState.READY.value,
        PhaseState.IMPLEMENTING.value,
        PhaseState.CANDIDATE_CREATED.value,
        PhaseState.TESTING.value,
        PhaseState.CODE_REVIEW.value,
        PhaseState.QA_REVIEW.value,
        PhaseState.READY_FOR_DELIVERY.value,
        PhaseState.PUSHED.value,
        PhaseState.PR_OPEN.value,
        PhaseState.MERGE_PENDING_REMOTE.value,
        PhaseState.MERGED.value,
        PhaseState.COMPLETED.value,
        PhaseState.ROLLED_BACK.value,
        PhaseState.PAUSED.value,
        PhaseState.BLOCKED.value,
        PhaseState.AWAITING_MANUAL.value,
        PhaseState.AWAITING_APPROVAL.value,
    }
)

# Code-changing phase states requiring the repository lock (database-design.md §4)
ACTIVE_CODE_CHANGING_PHASE_STATES: frozenset[str] = frozenset(
    {
        PhaseState.PREPARING_WORKSPACE.value,
        PhaseState.IMPLEMENTING.value,
        PhaseState.CANDIDATE_CREATED.value,
        PhaseState.TESTING.value,
        PhaseState.CODE_REVIEW.value,
        PhaseState.QA_REVIEW.value,
        PhaseState.AWAITING_MANUAL.value,
        PhaseState.AWAITING_APPROVAL.value,
        PhaseState.FIXING.value,
        PhaseState.READY_FOR_DELIVERY.value,
        PhaseState.PUSHED.value,
        PhaseState.PR_OPEN.value,
        PhaseState.CI_VALIDATION.value,
        PhaseState.MERGING.value,
        PhaseState.MERGE_PENDING_REMOTE.value,
        PhaseState.CANCELLING_REMOTE_MERGE.value,
    }
)


def is_task_transition_allowed(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in TASK_ALLOWED_TRANSITIONS


def is_phase_transition_allowed(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in PHASE_ALLOWED_TRANSITIONS
