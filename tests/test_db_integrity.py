"""Slice 1.2 tests: SQLite WAL pragmas, append-only triggers, composite FKs (ATLAS-014), and paired backups."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from atlas.db.connection import (
    DatabaseManager,
    PairedBackupError,
    RepositoryIntegrityError,
    insert_execution_checked,
    insert_review_round_checked,
    validate_phase_dependencies,
)
from atlas.db.migrate import run_migrations
from atlas.utils import new_ulid, utc_now_iso


def _seed_project_task_plan_phase(
    conn: sqlite3.Connection,
    *,
    project_id: str | None = None,
    repo: str | None = None,
) -> tuple[str, str, str, str]:
    now = utc_now_iso()
    pid = project_id or new_ulid()
    tid = new_ulid()
    plid = new_ulid()
    phid = new_ulid()
    blob_sha = "a" * 64

    conn.execute(
        """
        INSERT OR IGNORE INTO blobs (sha256, size, media_type, redacted, created_at)
        VALUES (?, 10, 'application/json', 1, ?);
        """,
        (blob_sha, now),
    )
    conn.execute(
        """
        INSERT INTO projects (
          id, name, github_repo, visibility, base_branch,
          protection_status, merge_queue, clone_path, config_json,
          policy_version, status, created_at, updated_at
        ) VALUES (?, ?, ?, 'private', 'main', 'unknown', 0, '/tmp/repo', '{}', 1, 'active', ?, ?);
        """,
        (pid, f"proj-{pid[-6:]}", repo or f"owner/repo-{pid[-6:].lower()}", now, now),
    )
    conn.execute(
        """
        INSERT INTO tasks (
          id, project_id, title, request_text, category, priority,
          autonomy, acceptance_criteria_json, state, version, created_at, updated_at
        ) VALUES (?, ?, 'Title', 'Req', 'feature', 3, 'supervised', '[]', 'CREATED', 0, ?, ?);
        """,
        (tid, pid, now, now),
    )
    conn.execute(
        """
        INSERT INTO plans (
          id, project_id, task_id, revision, author_agent, author_model,
          author_provider, author_family, author_agent_revision, base_sha,
          plan_blob, status, created_at
        ) VALUES (?, ?, ?, 1, 'claude', 'claude-sonnet-4-5', 'anthropic', 'anthropic', 1, 'deadbeef', ?, 'approved', ?);
        """,
        (plid, pid, tid, blob_sha, now),
    )
    conn.execute(
        """
        INSERT INTO phases (
          id, project_id, task_id, plan_id, seq, kind, title, objective,
          spec_json, state, version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, 'change', 'Phase 1', 'Obj', '{}', 'PENDING', 0, ?, ?);
        """,
        (phid, pid, tid, plid, now, now),
    )
    return pid, tid, plid, phid


def test_pragmas_and_file_permissions(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    pragmas = db.verify_pragmas()
    assert pragmas["journal_mode"] == "wal"
    assert pragmas["synchronous"] == 2  # FULL
    assert pragmas["foreign_keys"] == 1
    assert pragmas["busy_timeout"] == 5000

    dir_mode = os.stat(db.data_dir).st_mode & 0o777
    atlas_mode = os.stat(db.atlas_db_path).st_mode & 0o777
    graph_mode = os.stat(db.graph_db_path).st_mode & 0o777
    assert dir_mode == 0o700
    assert atlas_mode == 0o600
    assert graph_mode == 0o600
    assert db.run_quick_check() is True


def test_alembic_baseline_migration(tmp_path: Path) -> None:
    db_file = tmp_path / "migrated.db"
    run_migrations(db_file)
    conn = sqlite3.connect(str(db_file))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        assert "projects" in tables
        assert "tasks" in tables
        assert "phases" in tables
        assert "candidates" in tables
        assert "transitions" in tables
        assert "audit_log" in tables
        assert "workflow_steps" in tables
    finally:
        conn.close()


def test_append_only_triggers_on_transitions_audit_and_agent_revisions(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    now = utc_now_iso()

    with db.begin_immediate() as conn:
        pid, tid, _, _ = _seed_project_task_plan_phase(conn)
        tr_id = new_ulid()
        au_id = new_ulid()
        conn.execute(
            """
            INSERT INTO transitions (
              id, project_id, entity_type, entity_id, from_state, to_state,
              reason, actor, policy_version, correlation_id, at
            ) VALUES (?, ?, 'task', ?, 'CREATED', 'PAUSED', 'test', 'user', 1, ?, ?);
            """,
            (tr_id, pid, tid, new_ulid(), now),
        )
        conn.execute(
            """
            INSERT INTO audit_log (id, project_id, actor, action, detail_json, correlation_id, at)
            VALUES (?, ?, 'user', 'test_action', '{}', ?, ?);
            """,
            (au_id, pid, new_ulid(), now),
        )
        conn.execute(
            """
            INSERT INTO agents (
              id, integration_mode, adapter, current_revision, capabilities_json,
              auth_status, capacity_state, updated_at
            ) VALUES ('claude', 'headless', 'claude_cli', 1, '{}', 'ok', 'AVAILABLE', ?);
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO agent_revisions (
              agent_id, revision, adapter, billing, provider, models_json, created_at
            ) VALUES ('claude', 1, 'claude_cli', 'subscription', 'anthropic', '["claude-sonnet-4-5"]', ?);
            """,
            (now,),
        )

    # Attempt UPDATE and DELETE on transitions, audit_log, and agent_revisions -> all must raise IntegrityError
    for stmt in (
        f"UPDATE transitions SET reason = 'tampered' WHERE id = '{tr_id}';",
        f"DELETE FROM transitions WHERE id = '{tr_id}';",
        f"UPDATE audit_log SET action = 'tampered' WHERE id = '{au_id}';",
        f"DELETE FROM audit_log WHERE id = '{au_id}';",
        "UPDATE agent_revisions SET provider = 'openai' WHERE agent_id = 'claude';",
        "DELETE FROM agent_revisions WHERE agent_id = 'claude';",
    ):
        with pytest.raises(sqlite3.IntegrityError), db.begin_immediate() as conn:
            conn.execute(stmt)


def test_cross_project_and_cross_candidate_composite_fk_negatives(tmp_path: Path) -> None:
    """Negative tests for ATLAS-014 composite ownership FKs and repository validators."""
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    now = utc_now_iso()

    with db.begin_immediate() as conn:
        p1, t1, pl1, ph1 = _seed_project_task_plan_phase(conn)
        p2, t2, pl2, ph2 = _seed_project_task_plan_phase(conn)

        # Create two candidates on ph1: cand_a and cand_b, plus one candidate on ph2: cand_p2
        cand_a = new_ulid()
        cand_b = new_ulid()
        cand_p2 = new_ulid()
        for cid, proj, ph, rev, sha in (
            (cand_a, p1, ph1, 1, "111111"),
            (cand_b, p1, ph1, 2, "222222"),
            (cand_p2, p2, ph2, 1, "333333"),
        ):
            conn.execute(
                """
                INSERT INTO candidates (
                  id, project_id, phase_id, revision, commit_sha, tree_sha, parent_sha,
                  author_agent, guard_status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'tree', 'parent', 'codex', 'clear', ?);
                """,
                (cid, proj, ph, rev, sha, now),
            )

        # Create executions for cand_a and cand_b
        exec_a = new_ulid()
        exec_b = new_ulid()
        for eid, cid in ((exec_a, cand_a), (exec_b, cand_b)):
            insert_execution_checked(
                conn,
                {
                    "id": eid,
                    "project_id": p1,
                    "task_id": t1,
                    "phase_id": ph1,
                    "candidate_id": cid,
                    "kind": "test",
                    "status": "succeeded",
                    "argv_json": "[]",
                    "correlation_id": new_ulid(),
                    "created_at": now,
                },
            )

    # 1. Phase in project 1 pointing at task from project 2 -> FK error
    with pytest.raises(sqlite3.IntegrityError), db.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO phases (
              id, project_id, task_id, plan_id, seq, kind, title, objective,
              spec_json, state, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 99, 'change', 'Bad', 'Obj', '{}', 'PENDING', 0, ?, ?);
            """,
            (new_ulid(), p1, t2, pl1, now, now),
        )

    # 2. Phase in task 1 pointing at a plan from task 2 (same project) -> FK error
    with pytest.raises(sqlite3.IntegrityError), db.begin_immediate() as conn:
        t1_other = new_ulid()
        conn.execute(
            """
            INSERT INTO tasks (
              id, project_id, title, request_text, category, priority,
              autonomy, acceptance_criteria_json, state, version, created_at, updated_at
            ) VALUES (?, ?, 'Other', 'Req', 'feature', 3, 'supervised', '[]', 'CREATED', 0, ?, ?);
            """,
            (t1_other, p1, now, now),
        )
        conn.execute(
            """
            INSERT INTO phases (
              id, project_id, task_id, plan_id, seq, kind, title, objective,
              spec_json, state, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 99, 'change', 'BadPlan', 'Obj', '{}', 'PENDING', 0, ?, ?);
            """,
            (new_ulid(), p1, t1_other, pl1, now, now),
        )

    # 3. test_run for cand_a pointing at execution of cand_b -> composite FK error
    with pytest.raises(sqlite3.IntegrityError), db.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO test_runs (
              id, project_id, phase_id, candidate_id, execution_id,
              command, exit_code, evidence, integrity, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pytest', 0, 'executed', 'verified', ?);
            """,
            (new_ulid(), p1, ph1, cand_a, exec_b, now),
        )

    # 4. review_round for cand_a pointing at execution of cand_b -> composite FK / validator error
    with pytest.raises((sqlite3.IntegrityError, RepositoryIntegrityError)), db.begin_immediate() as conn:
        insert_review_round_checked(
            conn,
            {
                "id": new_ulid(),
                "project_id": p1,
                "subject_type": "candidate",
                "plan_id": None,
                "candidate_id": cand_a,
                "kind": "code_review",
                "reviewer_agent": "claude",
                "author_agent": "codex",
                "reviewer_provider": "anthropic",
                "reviewer_family": "anthropic",
                "author_provider": "openai",
                "author_family": "openai",
                "verdict": "pass",
                "evidence": "agent_reported",
                "execution_id": exec_b,
                "cycle": 1,
                "created_at": now,
            },
        )

    # 5. handoff for cand_a pointing at execution of cand_b -> composite FK error
    with pytest.raises(sqlite3.IntegrityError), db.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO handoffs (
              id, project_id, phase_id, candidate_id, execution_id,
              agent_id, step, nonce, packet_path, status, expires_at
            ) VALUES (?, ?, ?, ?, ?, 'antigravity', 'qa', ?, '/tmp/pkt', 'open', ?);
            """,
            (new_ulid(), p1, ph1, cand_a, exec_b, new_ulid(), now),
        )

    # 6. Trigger check: phases.current_candidate_id set to a candidate from another phase
    with pytest.raises(sqlite3.IntegrityError), db.begin_immediate() as conn:
        conn.execute(
            "UPDATE phases SET current_candidate_id = ? WHERE id = ?;",
            (cand_p2, ph1),
        )


def test_phase_dependency_dag_validation(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    now = utc_now_iso()

    with db.begin_immediate() as conn:
        p1, t1, pl1, ph1 = _seed_project_task_plan_phase(conn)
        ph2 = new_ulid()
        conn.execute(
            """
            INSERT INTO phases (
              id, project_id, task_id, plan_id, seq, kind, title, objective,
              spec_json, state, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 2, 'change', 'Phase 2', 'Obj', '{}', 'PENDING', 0, ?, ?);
            """,
            (ph2, p1, t1, pl1, now, now),
        )
        # Valid dependency ph2 -> ph1
        conn.execute(
            "INSERT INTO phase_dependencies (project_id, phase_id, depends_on) VALUES (?, ?, ?);",
            (p1, ph2, ph1),
        )
        validate_phase_dependencies(conn, p1, t1)

        # Introduce cycle ph1 -> ph2
        conn.execute(
            "INSERT INTO phase_dependencies (project_id, phase_id, depends_on) VALUES (?, ?, ?);",
            (p1, ph1, ph2),
        )
        with pytest.raises(RepositoryIntegrityError, match="Cycle detected"):
            validate_phase_dependencies(conn, p1, t1)


def test_paired_backup_and_restore_generation_enforcement(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()

    with db.begin_immediate() as conn:
        _seed_project_task_plan_phase(conn, repo="owner/backup-test")

    gen_id, backup_dir = db.create_paired_backup("pre-upgrade")
    assert gen_id.startswith("pre-upgrade-")

    # Restore succeeds when generations match
    restored_gen = db.restore_paired_backup(backup_dir)
    assert restored_gen == gen_id

    # Tamper with graph.db backup_generation and verify restore refuses mismatched generations (ATLAS-L05)
    g_conn = sqlite3.connect(str(backup_dir / "graph.db"))
    try:
        g_conn.execute("UPDATE backup_metadata SET backup_generation = 'wrong-gen';")
        g_conn.commit()
    finally:
        g_conn.close()

    with pytest.raises(PairedBackupError, match="Mismatched backup_generation"):
        db.restore_paired_backup(backup_dir)
