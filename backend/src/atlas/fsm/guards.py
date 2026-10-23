"""Pure policy-engine guard functions bound to candidate IDs (docs/workflow-state-machine.md §3.1)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from atlas.config.schemas import check_independence


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    guard_id: str
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    blocked_reason: str | None = None


@dataclass(frozen=True)
class GuardContext:
    """External/runtime inputs passed into pure policy engine checks."""

    worktree_clean: bool = True
    worktree_branch_valid: bool = True
    head_matches_base: bool = True
    process_group_gone: bool = True
    ls_remote_sha: str | None = None
    pr_head_sha: str | None = None
    pr_exists: bool = False
    pr_state: str | None = None
    pr_mergeable: str | None = None
    pr_review_decision_ok: bool = True
    ci_checks_status: dict[str, str] = field(default_factory=dict)
    merged_base_branch: str | None = None
    merged_repo: str | None = None
    merge_commit_oid: str | None = None
    merge_commit_reachable_on_base: bool = False


def _is_not_expired(expires_at_iso: str, now_iso: str | None = None) -> bool:
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(UTC)
    exp = datetime.fromisoformat(expires_at_iso)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp > now


def check_g_dep(conn: sqlite3.Connection, phase_id: str) -> GuardResult:
    """G-DEP (PENDING -> READY): All dependencies COMPLETED, project not PAUSED, repo lock available."""
    phase = conn.execute(
        "SELECT id, project_id, task_id FROM phases WHERE id = ?;", (phase_id,)
    ).fetchone()
    if not phase:
        return GuardResult(False, "G-DEP", f"Phase {phase_id!r} not found")

    project = conn.execute(
        "SELECT id, status FROM projects WHERE id = ?;", (phase["project_id"],)
    ).fetchone()
    if not project or project["status"] == "paused":
        return GuardResult(False, "G-DEP", "Project is paused")

    deps = conn.execute(
        """
        SELECT p.id, p.state
        FROM phase_dependencies pd
        JOIN phases p ON p.id = pd.depends_on AND p.project_id = pd.project_id
        WHERE pd.phase_id = ? AND pd.project_id = ?;
        """,
        (phase_id, phase["project_id"]),
    ).fetchall()
    for dep in deps:
        if dep["state"] != "COMPLETED":
            return GuardResult(
                False,
                "G-DEP",
                f"Dependency phase {dep['id']} is in state {dep['state']} (must be COMPLETED)",
            )

    lock = conn.execute(
        "SELECT phase_id FROM repo_locks WHERE project_id = ?;",
        (phase["project_id"],),
    ).fetchone()
    if lock and lock["phase_id"] != phase_id:
        return GuardResult(
            False,
            "G-DEP",
            f"Repository lock is held by phase {lock['phase_id']}",
        )

    return GuardResult(True, "G-DEP", "Dependencies satisfied and repo lock available")


def check_g_ws(
    conn: sqlite3.Connection, phase_id: str, ctx: GuardContext | None = None
) -> GuardResult:
    """G-WS (PREPARING_WORKSPACE -> IMPLEMENTING): Worktree on atlas/... branch, HEAD == base_sha, clean, repo lock held."""
    ctx = ctx or GuardContext()
    phase = conn.execute(
        "SELECT id, project_id, branch, base_sha FROM phases WHERE id = ?;", (phase_id,)
    ).fetchone()
    if not phase:
        return GuardResult(False, "G-WS", f"Phase {phase_id!r} not found")

    lock = conn.execute(
        "SELECT phase_id FROM repo_locks WHERE project_id = ?;",
        (phase["project_id"],),
    ).fetchone()
    if not lock or lock["phase_id"] != phase_id:
        return GuardResult(False, "G-WS", "Repository lock is not held by this phase")

    branch = str(phase["branch"] or "")
    if not branch.startswith("atlas/") or not ctx.worktree_branch_valid:
        return GuardResult(False, "G-WS", f"Invalid phase branch {branch!r}")

    if not phase["base_sha"] or not ctx.head_matches_base:
        return GuardResult(False, "G-WS", "Worktree HEAD does not match freshly fetched base_sha")

    if not ctx.worktree_clean:
        return GuardResult(False, "G-WS", "Worktree is not clean")

    return GuardResult(True, "G-WS", "Workspace prepared and repo lock verified")


def check_g_cap(conn: sqlite3.Connection, agent_id: str) -> GuardResult:
    """G-CAP: Assigned agent capacity is AVAILABLE."""
    agent = conn.execute(
        "SELECT id, capacity_state, integration_mode FROM agents WHERE id = ?;",
        (agent_id,),
    ).fetchone()
    if not agent:
        return GuardResult(False, "G-CAP", f"Agent {agent_id!r} not registered")
    if agent["integration_mode"] == "blocked":
        return GuardResult(False, "G-CAP", f"Agent {agent_id!r} is blocked")
    if agent["capacity_state"] not in ("AVAILABLE", "MANUAL_ONLY"):
        return GuardResult(
            False,
            "G-CAP",
            f"Agent {agent_id!r} capacity state is {agent['capacity_state']}",
        )
    return GuardResult(True, "G-CAP", f"Agent {agent_id!r} available")


def check_g_ind(
    author_provider: str,
    author_family: str,
    reviewer_provider: str,
    reviewer_family: str,
) -> GuardResult:
    """G-IND: Reviewer's derived provider != author's (or both local) AND model_family != author_family (ADR-005)."""
    if check_independence(author_provider, author_family, reviewer_provider, reviewer_family):
        return GuardResult(
            True,
            "G-IND",
            f"Reviewer ({reviewer_provider}/{reviewer_family}) is independent of author ({author_provider}/{author_family})",
        )
    return GuardResult(
        False,
        "G-IND",
        f"Reviewer ({reviewer_provider}/{reviewer_family}) is NOT independent of author ({author_provider}/{author_family})",
        blocked_reason="agent_config",
    )


def check_g_cand(
    conn: sqlite3.Connection, phase_id: str, ctx: GuardContext | None = None
) -> GuardResult:
    """G-CAND (IMPLEMENTING/FIXING -> CANDIDATE_CREATED): Execution succeeded, pgid gone, candidate created & clean."""
    ctx = ctx or GuardContext()
    phase = conn.execute(
        "SELECT id, project_id, current_candidate_id FROM phases WHERE id = ?;",
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-CAND", "Phase has no current_candidate_id")

    cand = conn.execute(
        "SELECT id, created_by_execution, guard_status FROM candidates WHERE id = ? AND phase_id = ?;",
        (phase["current_candidate_id"], phase_id),
    ).fetchone()
    if not cand:
        return GuardResult(False, "G-CAND", "Current candidate record not found")

    if cand["created_by_execution"]:
        ex = conn.execute(
            "SELECT id, status, exit_code FROM executions WHERE id = ?;",
            (cand["created_by_execution"],),
        ).fetchone()
        if not ex or ex["status"] != "succeeded" or ex["exit_code"] != 0:
            return GuardResult(False, "G-CAND", "Authoring execution did not exit 0 with succeeded status")

    if not ctx.process_group_gone:
        return GuardResult(False, "G-CAND", "Agent process group is still alive")

    if not ctx.worktree_clean:
        return GuardResult(False, "G-CAND", "Worktree is not clean after candidate commit")

    return GuardResult(
        True,
        "G-CAND",
        f"Candidate {cand['id']} created with guard_status={cand['guard_status']}",
        evidence_refs=[str(cand["id"])],
    )


def check_g_diff(
    conn: sqlite3.Connection, phase_id: str, now_iso: str | None = None
) -> GuardResult:
    """G-DIFF (CANDIDATE_CREATED/AWAITING_APPROVAL -> TESTING): Every violation kind on current candidate has approved approval bound to candidate_id and policy_version (ATLAS-201)."""
    phase = conn.execute(
        """
        SELECT p.id, p.project_id, p.current_candidate_id, pr.policy_version
        FROM phases p
        JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?;
        """,
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-DIFF", "Phase has no current candidate")

    candidate_id = str(phase["current_candidate_id"])
    policy_version = int(phase["policy_version"])

    violations = conn.execute(
        "SELECT DISTINCT kind FROM candidate_guard_violations WHERE candidate_id = ? AND project_id = ?;",
        (candidate_id, phase["project_id"]),
    ).fetchall()
    if not violations:
        return GuardResult(True, "G-DIFF", "No diff-guard violations on candidate", evidence_refs=[candidate_id])

    approval_refs: list[str] = []
    for v_row in violations:
        v_kind = str(v_row["kind"])
        approvals = conn.execute(
            """
            SELECT id, expires_at
            FROM approvals
            WHERE project_id = ?
              AND phase_id = ?
              AND candidate_id = ?
              AND kind = ?
              AND policy_version = ?
              AND status = 'approved';
            """,
            (phase["project_id"], phase_id, candidate_id, v_kind, policy_version),
        ).fetchall()
        valid_app = next(
            (a for a in approvals if _is_not_expired(str(a["expires_at"]), now_iso)),
            None,
        )
        if not valid_app:
            return GuardResult(
                False,
                "G-DIFF",
                f"Missing valid candidate-bound approval for violation kind {v_kind!r} on candidate {candidate_id}",
            )
        approval_refs.append(str(valid_app["id"]))

    return GuardResult(
        True,
        "G-DIFF",
        "All diff-guard violations approved for current candidate and policy_version",
        evidence_refs=approval_refs,
    )


def check_g_test(
    conn: sqlite3.Connection,
    phase_id: str,
    *,
    required_commands: list[str] | None = None,
    require_tests: bool = True,
) -> GuardResult:
    """G-TEST (TESTING -> CODE_REVIEW): Every required test command has an executed test_run for current candidate with exit 0 and integrity=verified."""
    phase = conn.execute(
        "SELECT id, project_id, current_candidate_id, spec_json FROM phases WHERE id = ?;",
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-TEST", "Phase has no current candidate")

    candidate_id = str(phase["current_candidate_id"])
    if required_commands is None:
        try:
            spec = json.loads(phase["spec_json"] or "{}")
            required_commands = list(spec.get("required_tests") or [])
        except json.JSONDecodeError:
            required_commands = []

    if require_tests and not required_commands:
        return GuardResult(
            False,
            "G-TEST",
            "require_tests is True but required test command set is empty",
            blocked_reason="no_test_sandbox",
        )

    evidence_refs: list[str] = []
    for cmd in required_commands:
        run = conn.execute(
            """
            SELECT id, exit_code, evidence, integrity
            FROM test_runs
            WHERE project_id = ?
              AND phase_id = ?
              AND candidate_id = ?
              AND command = ?
              AND evidence = 'executed'
              AND integrity = 'verified'
              AND exit_code = 0
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            (phase["project_id"], phase_id, candidate_id, cmd),
        ).fetchone()
        if not run:
            return GuardResult(
                False,
                "G-TEST",
                f"Missing executed & verified passing test_run for command {cmd!r} on candidate {candidate_id}",
            )
        evidence_refs.append(str(run["id"]))

    return GuardResult(
        True,
        "G-TEST",
        f"All {len(required_commands)} required test commands executed and verified on candidate {candidate_id}",
        evidence_refs=evidence_refs,
    )


def check_g_rev(
    conn: sqlite3.Connection,
    phase_id: str,
    *,
    block_on_high_findings: bool = True,
    now_iso: str | None = None,
) -> GuardResult:
    """G-REV (CODE_REVIEW -> QA_REVIEW): Independent code review exists for current candidate; no open CRITICAL/HIGH findings unless waived for this candidate."""
    phase = conn.execute(
        """
        SELECT p.id, p.project_id, p.current_candidate_id, pr.policy_version
        FROM phases p
        JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?;
        """,
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-REV", "Phase has no current candidate")

    candidate_id = str(phase["current_candidate_id"])
    policy_version = int(phase["policy_version"])

    reviews = conn.execute(
        """
        SELECT id, reviewer_provider, reviewer_family, author_provider, author_family
        FROM review_rounds
        WHERE project_id = ?
          AND subject_type = 'candidate'
          AND candidate_id = ?
          AND kind = 'code_review'
        ORDER BY cycle DESC;
        """,
        (phase["project_id"], candidate_id),
    ).fetchall()
    if not reviews:
        return GuardResult(
            False,
            "G-REV",
            f"No code_review round found for current candidate {candidate_id}",
        )

    latest_rev = reviews[0]
    ind = check_g_ind(
        str(latest_rev["author_provider"]),
        str(latest_rev["author_family"]),
        str(latest_rev["reviewer_provider"]),
        str(latest_rev["reviewer_family"]),
    )
    if not ind.ok:
        return GuardResult(False, "G-REV", f"Code review violates G-IND: {ind.reason}")

    # Check open blocking findings across any review_round on this phase
    blocking_severities = ("CRITICAL", "HIGH") if block_on_high_findings else ("CRITICAL",)
    placeholders = ", ".join("?" for _ in blocking_severities)
    open_findings = conn.execute(
        f"""
        SELECT f.id, f.severity, f.status
        FROM findings f
        JOIN review_rounds rr ON rr.id = f.review_round_id AND rr.project_id = f.project_id
        JOIN candidates c ON c.id = rr.candidate_id AND c.project_id = rr.project_id
        WHERE f.project_id = ?
          AND c.phase_id = ?
          AND f.severity IN ({placeholders})
          AND f.status NOT IN ('verified_fixed', 'rejected');
        """,
        (phase["project_id"], phase_id, *blocking_severities),
    ).fetchall()

    for finding in open_findings:
        if finding["status"] == "waived":
            # Verify candidate-bound waiver approval exists
            waiver = conn.execute(
                """
                SELECT id, expires_at
                FROM approvals
                WHERE project_id = ?
                  AND phase_id = ?
                  AND candidate_id = ?
                  AND kind = 'waiver'
                  AND policy_version = ?
                  AND status = 'approved';
                """,
                (phase["project_id"], phase_id, candidate_id, policy_version),
            ).fetchall()
            if any(_is_not_expired(str(w["expires_at"]), now_iso) for w in waiver):
                continue
        return GuardResult(
            False,
            "G-REV",
            f"Unresolved {finding['severity']} finding {finding['id']} (status={finding['status']})",
        )

    return GuardResult(
        True,
        "G-REV",
        f"Code review passed on candidate {candidate_id}",
        evidence_refs=[str(latest_rev["id"])],
    )


def check_g_fix(
    conn: sqlite3.Connection,
    finding_id: str,
    new_candidate_id: str,
    verification_round_id: str,
) -> GuardResult:
    """G-FIX: verified_fixed requires an independent reviewer re-check on a newer candidate + G-TEST on that candidate."""
    finding = conn.execute(
        """
        SELECT f.id, f.project_id, rr.candidate_id AS raised_candidate_id,
               c_old.phase_id, c_old.revision AS old_revision
        FROM findings f
        JOIN review_rounds rr ON rr.id = f.review_round_id AND rr.project_id = f.project_id
        JOIN candidates c_old ON c_old.id = rr.candidate_id AND c_old.project_id = rr.project_id
        WHERE f.id = ?;
        """,
        (finding_id,),
    ).fetchone()
    if not finding:
        return GuardResult(False, "G-FIX", f"Finding {finding_id!r} not found")

    c_new = conn.execute(
        "SELECT id, phase_id, revision, author_provider, author_family FROM candidates WHERE id = ? AND project_id = ?;",
        (new_candidate_id, finding["project_id"]),
    ).fetchone()
    if not c_new or c_new["phase_id"] != finding["phase_id"]:
        return GuardResult(False, "G-FIX", "New candidate does not belong to the finding's phase")

    if int(c_new["revision"]) <= int(finding["old_revision"]):
        return GuardResult(
            False,
            "G-FIX",
            f"Candidate revision {c_new['revision']} is not newer than finding revision {finding['old_revision']}",
        )

    v_round = conn.execute(
        """
        SELECT id, candidate_id, reviewer_provider, reviewer_family, verdict
        FROM review_rounds
        WHERE id = ? AND project_id = ?;
        """,
        (verification_round_id, finding["project_id"]),
    ).fetchone()
    if not v_round or v_round["candidate_id"] != new_candidate_id:
        return GuardResult(False, "G-FIX", "Verification review round is not bound to the new candidate")

    ind = check_g_ind(
        str(c_new["author_provider"]),
        str(c_new["author_family"]),
        str(v_round["reviewer_provider"]),
        str(v_round["reviewer_family"]),
    )
    if not ind.ok:
        return GuardResult(False, "G-FIX", f"Fix verifier is not independent: {ind.reason}")

    g_test = check_g_test(conn, str(c_new["phase_id"]))
    if not g_test.ok:
        return GuardResult(False, "G-FIX", f"G-TEST failed on new candidate: {g_test.reason}")

    return GuardResult(
        True,
        "G-FIX",
        f"Finding {finding_id} verified fixed on candidate {new_candidate_id}",
        evidence_refs=[verification_round_id, *g_test.evidence_refs],
    )


def check_g_qa(
    conn: sqlite3.Connection,
    phase_id: str,
    *,
    require_independent_qa: bool = True,
    block_on_high_findings: bool = True,
    now_iso: str | None = None,
) -> GuardResult:
    """G-QA (QA_REVIEW -> READY_FOR_DELIVERY): QA result for current candidate, source_integrity=verified, all criteria covered once & mandatory met, verdict='pass'."""
    phase = conn.execute(
        """
        SELECT p.id, p.project_id, p.current_candidate_id, p.spec_json, pr.policy_version
        FROM phases p
        JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?;
        """,
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-QA", "Phase has no current candidate")

    candidate_id = str(phase["current_candidate_id"])
    policy_version = int(phase["policy_version"])

    # Check if user waiver applies when require_independent_qa is False
    if not require_independent_qa:
        waivers = conn.execute(
            """
            SELECT id, expires_at FROM approvals
            WHERE project_id = ? AND phase_id = ? AND candidate_id = ?
              AND kind = 'waiver' AND policy_version = ? AND status = 'approved';
            """,
            (phase["project_id"], phase_id, candidate_id, policy_version),
        ).fetchall()
        valid_waiver = next(
            (w for w in waivers if _is_not_expired(str(w["expires_at"]), now_iso)),
            None,
        )
        if valid_waiver:
            return GuardResult(
                True,
                "G-QA",
                "QA waived by candidate-bound user approval",
                evidence_refs=[str(valid_waiver["id"])],
            )

    qa_round = conn.execute(
        """
        SELECT id, reviewer_provider, reviewer_family, author_provider, author_family,
               verdict, criteria_json, source_integrity
        FROM review_rounds
        WHERE project_id = ?
          AND subject_type = 'candidate'
          AND candidate_id = ?
          AND kind = 'qa'
        ORDER BY cycle DESC
        LIMIT 1;
        """,
        (phase["project_id"], candidate_id),
    ).fetchone()
    if not qa_round:
        return GuardResult(False, "G-QA", f"No QA review round found for candidate {candidate_id}")

    if qa_round["source_integrity"] != "verified":
        return GuardResult(
            False,
            "G-QA",
            f"QA worktree failed source integrity check ({qa_round['source_integrity']})",
        )

    ind = check_g_ind(
        str(qa_round["author_provider"]),
        str(qa_round["author_family"]),
        str(qa_round["reviewer_provider"]),
        str(qa_round["reviewer_family"]),
    )
    if not ind.ok:
        return GuardResult(False, "G-QA", f"QA review violates G-IND: {ind.reason}")

    # Validate criteria completeness (ATLAS-015)
    try:
        spec = json.loads(phase["spec_json"] or "{}")
        expected_criteria: list[dict[str, Any]] = list(spec.get("acceptance_criteria") or [])
        reported_criteria: list[dict[str, Any]] = json.loads(qa_round["criteria_json"] or "[]")
    except json.JSONDecodeError:
        return GuardResult(False, "G-QA", "Invalid JSON in phase spec or QA criteria_json")

    expected_ids = [str(c["id"]) for c in expected_criteria]
    reported_ids = [str(c.get("id")) for c in reported_criteria]
    if len(reported_ids) != len(set(reported_ids)) or set(reported_ids) != set(expected_ids):
        return GuardResult(
            False,
            "G-QA",
            f"QA criteria IDs {reported_ids} do not cover expected phase criteria {expected_ids} exactly once",
        )

    mandatory_ids = {
        str(c["id"]) for c in expected_criteria if c.get("mandatory", True)
    }
    reported_by_id = {str(c["id"]): str(c.get("status")) for c in reported_criteria}
    for mid in mandatory_ids:
        if reported_by_id.get(mid) != "met":
            return GuardResult(
                False,
                "G-QA",
                f"Mandatory acceptance criterion {mid!r} is not 'met' (got {reported_by_id.get(mid)!r})",
            )

    if str(qa_round["verdict"]).lower() != "pass":
        return GuardResult(
            False,
            "G-QA",
            f"QA verdict is {qa_round['verdict']!r} (expected 'pass')",
        )

    # Ensure no unresolved blocking findings across review and QA
    rev_check = check_g_rev(
        conn,
        phase_id,
        block_on_high_findings=block_on_high_findings,
        now_iso=now_iso,
    )
    if not rev_check.ok:
        return GuardResult(False, "G-QA", f"Blocking findings remain open: {rev_check.reason}")

    return GuardResult(
        True,
        "G-QA",
        f"QA passed and verified on candidate {candidate_id}",
        evidence_refs=[str(qa_round["id"])],
    )


def check_g_push(
    conn: sqlite3.Connection, phase_id: str, ctx: GuardContext | None = None
) -> GuardResult:
    """G-PUSH (READY_FOR_DELIVERY -> PUSHED): Outbox push confirmed and ls-remote branch == candidate commit SHA."""
    ctx = ctx or GuardContext()
    phase = conn.execute(
        "SELECT id, project_id, current_candidate_id FROM phases WHERE id = ?;",
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-PUSH", "Phase has no current candidate")

    cand = conn.execute(
        "SELECT id, commit_sha FROM candidates WHERE id = ?;",
        (phase["current_candidate_id"],),
    ).fetchone()
    if not cand:
        return GuardResult(False, "G-PUSH", "Candidate not found")

    push_op = conn.execute(
        """
        SELECT id FROM outbox_ops
        WHERE project_id = ? AND phase_id = ? AND candidate_id = ?
          AND op = 'push' AND status = 'confirmed';
        """,
        (phase["project_id"], phase_id, cand["id"]),
    ).fetchone()
    if not push_op:
        return GuardResult(False, "G-PUSH", "No confirmed outbox push op for current candidate")

    if ctx.ls_remote_sha != cand["commit_sha"]:
        return GuardResult(
            False,
            "G-PUSH",
            f"ls-remote SHA {ctx.ls_remote_sha!r} != candidate commit_sha {cand['commit_sha']!r}",
        )

    return GuardResult(
        True,
        "G-PUSH",
        "Push confirmed against remote branch SHA",
        evidence_refs=[str(push_op["id"])],
    )


def check_g_pr(
    conn: sqlite3.Connection, phase_id: str, ctx: GuardContext | None = None
) -> GuardResult:
    """G-PR (PUSHED -> PR_OPEN): Single PR exists and headRefOid == candidate commit SHA."""
    ctx = ctx or GuardContext()
    phase = conn.execute(
        "SELECT id, project_id, current_candidate_id, pr_number FROM phases WHERE id = ?;",
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-PR", "Phase has no current candidate")

    cand = conn.execute(
        "SELECT id, commit_sha FROM candidates WHERE id = ?;",
        (phase["current_candidate_id"],),
    ).fetchone()
    if not cand:
        return GuardResult(False, "G-PR", "Candidate not found")

    if not ctx.pr_exists or not phase["pr_number"]:
        return GuardResult(False, "G-PR", "Phase PR does not exist")

    if ctx.pr_head_sha != cand["commit_sha"]:
        return GuardResult(
            False,
            "G-PR",
            f"PR headRefOid {ctx.pr_head_sha!r} != candidate commit_sha {cand['commit_sha']!r}",
        )

    return GuardResult(True, "G-PR", f"PR #{phase['pr_number']} open at candidate {cand['commit_sha']}")


def check_g_ci(
    conn: sqlite3.Connection,
    phase_id: str,
    ctx: GuardContext | None = None,
    *,
    block_on_high_findings: bool = True,
    now_iso: str | None = None,
) -> GuardResult:
    """G-CI (CI_VALIDATION -> AWAITING_APPROVAL/MERGING): Branch protection known/user_declared, required checks SUCCESS on candidate SHA, mergeable=MERGEABLE, findings clear."""
    ctx = ctx or GuardContext()
    phase = conn.execute(
        """
        SELECT p.id, p.project_id, p.current_candidate_id,
               pr.protection_status, pr.protection_json, pr.config_json
        FROM phases p
        JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?;
        """,
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-CI", "Phase has no current candidate")

    cand = conn.execute(
        "SELECT id, commit_sha FROM candidates WHERE id = ?;",
        (phase["current_candidate_id"],),
    ).fetchone()
    if not cand:
        return GuardResult(False, "G-CI", "Candidate not found")

    if ctx.pr_head_sha != cand["commit_sha"]:
        return GuardResult(
            False,
            "G-CI",
            f"CI check head {ctx.pr_head_sha!r} != candidate SHA {cand['commit_sha']!r}",
        )

    # Determine required checks from protection_json or project config
    required_checks: list[str] = []
    if phase["protection_status"] == "known" and phase["protection_json"]:
        prot = json.loads(phase["protection_json"])
        required_checks = list(prot.get("required_checks") or [])
    elif phase["protection_status"] == "user_declared":
        cfg = json.loads(phase["config_json"] or "{}")
        required_checks = list(cfg.get("ci", {}).get("required_checks") or [])
    else:
        return GuardResult(
            False,
            "G-CI",
            "Branch protection is unknown and no user_declared required checks exist (ATLAS-010)",
        )

    for check_name in required_checks:
        if ctx.ci_checks_status.get(check_name) != "SUCCESS":
            return GuardResult(
                False,
                "G-CI",
                f"Required CI check {check_name!r} is {ctx.ci_checks_status.get(check_name)!r} (expected 'SUCCESS')",
            )

    if not ctx.pr_review_decision_ok:
        return GuardResult(False, "G-CI", "GitHub reviewDecision requirement not satisfied")

    if ctx.pr_mergeable != "MERGEABLE":
        return GuardResult(
            False,
            "G-CI",
            f"PR mergeable state is {ctx.pr_mergeable!r} (expected 'MERGEABLE')",
        )

    rev_check = check_g_rev(
        conn,
        phase_id,
        block_on_high_findings=block_on_high_findings,
        now_iso=now_iso,
    )
    if not rev_check.ok:
        return GuardResult(False, "G-CI", f"Blocking findings not clear: {rev_check.reason}")

    return GuardResult(True, "G-CI", f"CI green and mergeable on candidate {cand['commit_sha']}")


def check_g_merge_pre(
    conn: sqlite3.Connection,
    phase_id: str,
    ctx: GuardContext | None = None,
    *,
    require_tests: bool = True,
    require_independent_qa: bool = True,
    block_on_high_findings: bool = True,
    now_iso: str | None = None,
) -> GuardResult:
    """G-MERGE-PRE: Re-evaluates G-TEST, G-REV, G-QA, G-DIFF, G-CI, repo lock held, and unexpired merge approval bound to exact candidate_id and policy_version (ATLAS-007, D6)."""
    ctx = ctx or GuardContext()
    phase = conn.execute(
        """
        SELECT p.id, p.project_id, p.current_candidate_id, pr.policy_version
        FROM phases p
        JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?;
        """,
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-MERGE-PRE", "Phase has no current candidate")

    candidate_id = str(phase["current_candidate_id"])
    policy_version = int(phase["policy_version"])

    # Repo lock must be held by this phase
    lock = conn.execute(
        "SELECT phase_id FROM repo_locks WHERE project_id = ?;",
        (phase["project_id"],),
    ).fetchone()
    if not lock or lock["phase_id"] != phase_id:
        return GuardResult(False, "G-MERGE-PRE", "Repository lock is not held by this phase")

    # Verify G-DIFF, G-TEST, G-REV, G-QA, and G-CI all hold for this exact candidate
    for check_res in (
        check_g_diff(conn, phase_id, now_iso=now_iso),
        check_g_test(conn, phase_id, require_tests=require_tests),
        check_g_rev(conn, phase_id, block_on_high_findings=block_on_high_findings, now_iso=now_iso),
        check_g_qa(
            conn,
            phase_id,
            require_independent_qa=require_independent_qa,
            block_on_high_findings=block_on_high_findings,
            now_iso=now_iso,
        ),
        check_g_ci(
            conn,
            phase_id,
            ctx,
            block_on_high_findings=block_on_high_findings,
            now_iso=now_iso,
        ),
    ):
        if not check_res.ok:
            return GuardResult(
                False,
                "G-MERGE-PRE",
                f"Pre-merge check failed at {check_res.guard_id}: {check_res.reason}",
            )

    # Check candidate-bound, policy-version-bound, unexpired human merge approval (D6)
    approvals = conn.execute(
        """
        SELECT id, expires_at
        FROM approvals
        WHERE project_id = ?
          AND phase_id = ?
          AND candidate_id = ?
          AND kind = 'merge'
          AND policy_version = ?
          AND status = 'approved';
        """,
        (phase["project_id"], phase_id, candidate_id, policy_version),
    ).fetchall()
    valid_merge_app = next(
        (a for a in approvals if _is_not_expired(str(a["expires_at"]), now_iso)),
        None,
    )
    if not valid_merge_app:
        return GuardResult(
            False,
            "G-MERGE-PRE",
            f"No valid, unexpired merge approval bound to candidate {candidate_id} and policy_version {policy_version}",
        )

    return GuardResult(
        True,
        "G-MERGE-PRE",
        f"All pre-merge gates and human merge approval verified for candidate {candidate_id}",
        evidence_refs=[str(valid_merge_app["id"])],
    )


def check_g_merged(
    conn: sqlite3.Connection, phase_id: str, ctx: GuardContext | None = None
) -> GuardResult:
    """G-MERGED (-> MERGED): PR state=MERGED, headRefOid at merge == candidate commit_sha, base & repo match, mergeCommit reachable (ATLAS-009)."""
    ctx = ctx or GuardContext()
    phase = conn.execute(
        """
        SELECT p.id, p.project_id, p.current_candidate_id, pr.github_repo, pr.base_branch
        FROM phases p
        JOIN projects pr ON pr.id = p.project_id
        WHERE p.id = ?;
        """,
        (phase_id,),
    ).fetchone()
    if not phase or not phase["current_candidate_id"]:
        return GuardResult(False, "G-MERGED", "Phase has no current candidate")

    cand = conn.execute(
        "SELECT id, commit_sha FROM candidates WHERE id = ?;",
        (phase["current_candidate_id"],),
    ).fetchone()
    if not cand:
        return GuardResult(False, "G-MERGED", "Candidate not found")

    if ctx.pr_state != "MERGED":
        return GuardResult(False, "G-MERGED", f"PR state is {ctx.pr_state!r}, not 'MERGED'")

    if (
        ctx.pr_head_sha != cand["commit_sha"]
        or (ctx.merged_repo and ctx.merged_repo.lower() != str(phase["github_repo"]).lower())
        or (ctx.merged_base_branch and ctx.merged_base_branch != phase["base_branch"])
        or not ctx.merge_commit_oid
        or not ctx.merge_commit_reachable_on_base
    ):
        return GuardResult(
            False,
            "G-MERGED",
            f"External merge mismatch: merged head {ctx.pr_head_sha!r} vs candidate {cand['commit_sha']!r}",
            blocked_reason="external_merge_mismatch",
        )

    return GuardResult(
        True,
        "G-MERGED",
        f"Merge verified: head {cand['commit_sha']} -> merge commit {ctx.merge_commit_oid}",
        evidence_refs=[str(cand["id"])],
    )


def check_g_retry(retry_count: int, max_retries: int, *, is_reconciled: bool = True) -> GuardResult:
    """G-RETRY: retry_count < limit and operation is idempotent/reconciled."""
    if not is_reconciled:
        return GuardResult(False, "G-RETRY", "Operation is not reconciled", blocked_reason="effect_outcome_unknown")
    if retry_count >= max_retries:
        return GuardResult(
            False,
            "G-RETRY",
            f"Retry count {retry_count} reached limit {max_retries}",
            blocked_reason="repair_budget_exhausted",
        )
    return GuardResult(True, "G-RETRY", f"Retry {retry_count + 1}/{max_retries} permitted")
