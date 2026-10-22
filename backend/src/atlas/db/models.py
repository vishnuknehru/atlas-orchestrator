"""SQLAlchemy 2.x models and canonical SQLite DDL for ATLAS (docs/database-design.md)."""

from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for SQLAlchemy ORM mappings."""


class ProjectModel(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    github_repo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    visibility: Mapped[str] = mapped_column(String, nullable=False)
    base_branch: Mapped[str] = mapped_column(String, nullable=False, default="main")
    protection_status: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    protection_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    merge_queue: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clone_path: Mapped[str] = mapped_column(String, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    detected_stack_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    blocked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class TaskModel(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    autonomy: Mapped[str] = mapped_column(String, nullable=False, default="supervised")
    acceptance_criteria_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    resume_to: Mapped[str | None] = mapped_column(String, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    current_plan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pause_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    cancel_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    rebuild_target: Mapped[str | None] = mapped_column(String, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


# Canonical SQLite schema DDL matching docs/database-design.md §3 & §5 verbatim,
# including composite foreign keys (ATLAS-014) and append-only triggers.
SCHEMA_Statements: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY,
      name TEXT UNIQUE NOT NULL,
      github_repo TEXT NOT NULL UNIQUE,
      visibility TEXT NOT NULL CHECK (visibility IN ('private','public','internal')),
      base_branch TEXT NOT NULL DEFAULT 'main',
      protection_status TEXT NOT NULL DEFAULT 'unknown' CHECK (protection_status IN ('known','user_declared','unknown')),
      protection_json TEXT,
      merge_queue INTEGER NOT NULL DEFAULT 0,
      clone_path TEXT NOT NULL,
      config_json TEXT NOT NULL,
      policy_version INTEGER NOT NULL DEFAULT 1,
      detected_stack_json TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      blocked_reason TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id),
      title TEXT NOT NULL,
      request_text TEXT NOT NULL,
      category TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 3,
      autonomy TEXT NOT NULL DEFAULT 'supervised',
      acceptance_criteria_json TEXT NOT NULL,
      state TEXT NOT NULL,
      resume_to TEXT,
      blocked_reason TEXT,
      current_plan_id TEXT,
      pause_requested_at TEXT,
      cancel_requested_at TEXT,
      rebuild_target TEXT,
      generation INTEGER NOT NULL DEFAULT 1,
      version INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS blobs (
      sha256 TEXT PRIMARY KEY,
      size INTEGER NOT NULL,
      media_type TEXT NOT NULL,
      redacted INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      revision INTEGER NOT NULL,
      author_agent TEXT NOT NULL,
      author_model TEXT,
      author_provider TEXT NOT NULL,
      author_family TEXT NOT NULL,
      author_agent_revision INTEGER NOT NULL,
      authoring_execution_id TEXT,
      base_sha TEXT NOT NULL,
      plan_blob TEXT NOT NULL REFERENCES blobs(sha256),
      status TEXT NOT NULL CHECK (status IN ('draft','challenged','approved','superseded')),
      created_at TEXT NOT NULL,
      UNIQUE (task_id, revision),
      UNIQUE (id, project_id),
      UNIQUE (id, task_id, project_id),
      FOREIGN KEY (task_id, project_id) REFERENCES tasks(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS phases (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      plan_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      kind TEXT NOT NULL DEFAULT 'change' CHECK (kind IN ('change','revert')),
      reverts_phase_id TEXT,
      title TEXT NOT NULL,
      objective TEXT NOT NULL,
      spec_json TEXT NOT NULL,
      state TEXT NOT NULL,
      resume_to TEXT,
      blocked_reason TEXT,
      branch TEXT,
      worktree_path TEXT,
      base_sha TEXT,
      current_candidate_id TEXT,
      pr_number INTEGER,
      merge_commit_sha TEXT,
      repair_attempts INTEGER NOT NULL DEFAULT 0,
      review_cycles INTEGER NOT NULL DEFAULT 0,
      pause_requested_at TEXT,
      cancel_requested_at TEXT,
      rebuild_target TEXT,
      remote_merge_withdrawn INTEGER NOT NULL DEFAULT 0,
      generation INTEGER NOT NULL DEFAULT 1,
      version INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (plan_id, seq),
      UNIQUE (id, project_id),
      UNIQUE (id, task_id, project_id),
      FOREIGN KEY (task_id, project_id) REFERENCES tasks(id, project_id),
      FOREIGN KEY (plan_id, task_id, project_id) REFERENCES plans(id, task_id, project_id),
      FOREIGN KEY (reverts_phase_id, project_id) REFERENCES phases(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS phase_dependencies (
      project_id TEXT NOT NULL,
      phase_id TEXT NOT NULL,
      depends_on TEXT NOT NULL,
      PRIMARY KEY (phase_id, depends_on),
      CHECK (phase_id <> depends_on),
      FOREIGN KEY (phase_id, project_id) REFERENCES phases(id, project_id),
      FOREIGN KEY (depends_on, project_id) REFERENCES phases(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS candidates (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      phase_id TEXT NOT NULL,
      revision INTEGER NOT NULL,
      commit_sha TEXT NOT NULL,
      tree_sha TEXT NOT NULL,
      parent_sha TEXT NOT NULL,
      author_agent TEXT NOT NULL,
      author_model TEXT,
      author_provider TEXT NOT NULL DEFAULT 'unknown',
      author_family TEXT NOT NULL DEFAULT 'unknown',
      author_agent_revision INTEGER NOT NULL DEFAULT 1,
      created_by_execution TEXT,
      guard_status TEXT NOT NULL CHECK (guard_status IN ('clear','held')),
      guard_report_ref TEXT,
      created_at TEXT NOT NULL,
      UNIQUE (phase_id, revision),
      UNIQUE (phase_id, commit_sha),
      UNIQUE (id, project_id),
      UNIQUE (id, phase_id, project_id),
      FOREIGN KEY (phase_id, project_id) REFERENCES phases(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_guard_violations (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      candidate_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('protected_path','secret_change','destructive_migration')),
      path TEXT,
      detail TEXT NOT NULL,
      FOREIGN KEY (candidate_id, project_id) REFERENCES candidates(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS repo_locks (
      project_id TEXT PRIMARY KEY REFERENCES projects(id),
      phase_id TEXT NOT NULL,
      acquired_at TEXT NOT NULL,
      holder_instance TEXT NOT NULL,
      FOREIGN KEY (phase_id, project_id) REFERENCES phases(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
      id TEXT PRIMARY KEY,
      integration_mode TEXT NOT NULL CHECK (integration_mode IN ('headless','manual_handoff','blocked')),
      adapter TEXT NOT NULL,
      current_revision INTEGER NOT NULL,
      cli_version TEXT,
      help_hash TEXT,
      capabilities_json TEXT NOT NULL,
      auth_status TEXT NOT NULL,
      capacity_state TEXT NOT NULL,
      capacity_reason TEXT,
      next_probe_at TEXT,
      last_success_at TEXT,
      updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_revisions (
      agent_id TEXT NOT NULL REFERENCES agents(id),
      revision INTEGER NOT NULL,
      adapter TEXT NOT NULL,
      billing TEXT NOT NULL CHECK (billing IN ('subscription','api_key','local','manual')),
      provider TEXT NOT NULL,
      models_json TEXT NOT NULL,
      model_family_declared TEXT,
      endpoint TEXT,
      allow_remote_endpoint INTEGER NOT NULL DEFAULT 0,
      manifest_hash TEXT,
      created_at TEXT NOT NULL,
      PRIMARY KEY (agent_id, revision)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS capacity_events (
      id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL REFERENCES agents(id),
      from_state TEXT,
      to_state TEXT NOT NULL,
      signal TEXT NOT NULL,
      execution_id TEXT,
      at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS executions (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      phase_id TEXT,
      candidate_id TEXT,
      step_id TEXT,
      idempotency_key TEXT UNIQUE,
      kind TEXT NOT NULL CHECK (kind IN ('agent','test','git','gh')),
      agent_id TEXT REFERENCES agents(id),
      role TEXT,
      model TEXT,
      agent_revision INTEGER,
      provider TEXT,
      model_family TEXT,
      status TEXT NOT NULL CHECK (
        status IN ('launching','running','succeeded','failed','timed_out','cancelled','interrupted','awaiting_manual')
      ),
      retry_count INTEGER NOT NULL DEFAULT 0,
      git_sha TEXT,
      argv_json TEXT NOT NULL,
      pid INTEGER,
      pgid INTEGER,
      pid_start_time TEXT,
      run_file TEXT,
      provider_session_id TEXT,
      exit_code INTEGER,
      error_class TEXT,
      error_detail TEXT,
      capacity_signal TEXT,
      result_json TEXT,
      instance_generation INTEGER,
      correlation_id TEXT NOT NULL,
      started_at TEXT,
      finished_at TEXT,
      created_at TEXT NOT NULL,
      UNIQUE (id, project_id),
      UNIQUE (id, candidate_id, project_id),
      CHECK (kind <> 'agent' OR (provider IS NOT NULL AND model_family IS NOT NULL AND agent_revision IS NOT NULL)),
      FOREIGN KEY (task_id, project_id) REFERENCES tasks(id, project_id),
      FOREIGN KEY (phase_id, task_id, project_id) REFERENCES phases(id, task_id, project_id),
      FOREIGN KEY (candidate_id, phase_id, project_id) REFERENCES candidates(id, phase_id, project_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_exec_live
      ON executions(status) WHERE status IN ('launching','running');
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_refs (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id),
      sha256 TEXT NOT NULL REFERENCES blobs(sha256),
      kind TEXT NOT NULL,
      execution_id TEXT,
      direction TEXT CHECK (direction IN ('in','out')),
      created_at TEXT NOT NULL,
      UNIQUE (id, project_id),
      FOREIGN KEY (execution_id, project_id) REFERENCES executions(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS test_runs (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      phase_id TEXT NOT NULL,
      candidate_id TEXT NOT NULL,
      execution_id TEXT,
      command TEXT NOT NULL,
      exit_code INTEGER,
      passed INTEGER,
      failed INTEGER,
      skipped INTEGER,
      evidence TEXT NOT NULL CHECK (evidence IN ('executed','manual_imported','agent_reported')),
      integrity TEXT NOT NULL CHECK (integrity IN ('verified','invalid_source_mutated','not_checked')),
      report_ref TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY (candidate_id, phase_id, project_id) REFERENCES candidates(id, phase_id, project_id),
      FOREIGN KEY (execution_id, candidate_id, project_id) REFERENCES executions(id, candidate_id, project_id),
      CHECK (evidence <> 'executed' OR (execution_id IS NOT NULL AND integrity <> 'not_checked'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS review_rounds (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      subject_type TEXT NOT NULL CHECK (subject_type IN ('plan','candidate')),
      plan_id TEXT,
      candidate_id TEXT,
      kind TEXT NOT NULL CHECK (kind IN ('plan_challenge','code_review','qa','fix_verification')),
      reviewer_agent TEXT NOT NULL,
      reviewer_model TEXT,
      author_agent TEXT NOT NULL,
      author_model TEXT,
      reviewer_provider TEXT NOT NULL,
      reviewer_family TEXT NOT NULL,
      reviewer_agent_revision INTEGER,
      author_provider TEXT NOT NULL,
      author_family TEXT NOT NULL,
      author_agent_revision INTEGER,
      verdict TEXT NOT NULL,
      evidence TEXT NOT NULL CHECK (evidence IN ('agent_reported','manual_imported')),
      criteria_json TEXT,
      source_integrity TEXT NOT NULL DEFAULT 'verified' CHECK (source_integrity IN ('verified','invalid_source_mutated','not_checked')),
      execution_id TEXT,
      cycle INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE (id, project_id),
      CHECK (
        (subject_type='plan' AND plan_id IS NOT NULL AND candidate_id IS NULL)
        OR (subject_type='candidate' AND candidate_id IS NOT NULL AND plan_id IS NULL)
      ),
      FOREIGN KEY (plan_id, project_id) REFERENCES plans(id, project_id),
      FOREIGN KEY (candidate_id, project_id) REFERENCES candidates(id, project_id),
      FOREIGN KEY (execution_id, candidate_id, project_id) REFERENCES executions(id, candidate_id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      review_round_id TEXT NOT NULL,
      severity TEXT NOT NULL CHECK (severity IN ('CRITICAL','HIGH','MEDIUM','LOW','INFORMATIONAL')),
      category TEXT NOT NULL,
      file TEXT,
      location TEXT,
      description TEXT NOT NULL,
      evidence TEXT NOT NULL,
      recommendation TEXT,
      status TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN ('open','accepted','rejected','fixed_unverified','verified_fixed','waived')
      ),
      fingerprint TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (id, project_id),
      FOREIGN KEY (review_round_id, project_id) REFERENCES review_rounds(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS finding_responses (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      finding_id TEXT NOT NULL,
      responder TEXT NOT NULL,
      response TEXT NOT NULL CHECK (response IN ('accept','reject','fixed','waive')),
      justification TEXT NOT NULL,
      at_candidate_id TEXT,
      verified_by_round TEXT,
      approval_id TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY (finding_id, project_id) REFERENCES findings(id, project_id),
      FOREIGN KEY (verified_by_round, project_id) REFERENCES review_rounds(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      phase_id TEXT,
      task_id TEXT,
      kind TEXT NOT NULL CHECK (
        kind IN ('plan','merge','waiver','protected_path','secret_change','destructive_migration','override')
      ),
      candidate_id TEXT,
      plan_id TEXT,
      policy_version INTEGER NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired','superseded')),
      requested_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      decided_at TEXT,
      decided_by TEXT,
      reason TEXT,
      FOREIGN KEY (phase_id, project_id) REFERENCES phases(id, project_id),
      FOREIGN KEY (candidate_id, project_id) REFERENCES candidates(id, project_id),
      FOREIGN KEY (plan_id, project_id) REFERENCES plans(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS handoffs (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      phase_id TEXT,
      candidate_id TEXT,
      execution_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      step TEXT NOT NULL,
      nonce TEXT NOT NULL UNIQUE,
      packet_path TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('open','imported','rejected','expired','superseded')),
      expires_at TEXT NOT NULL,
      imported_at TEXT,
      declared_model TEXT,
      rejection_reason TEXT,
      FOREIGN KEY (candidate_id, phase_id, project_id) REFERENCES candidates(id, phase_id, project_id),
      FOREIGN KEY (execution_id, candidate_id, project_id) REFERENCES executions(id, candidate_id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox_ops (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      phase_id TEXT,
      candidate_id TEXT,
      step_id TEXT,
      op TEXT NOT NULL CHECK (
        op IN ('push','create_pr','update_pr','merge_pr','cancel_remote_merge','delete_branch','create_revert_pr','rerun_ci','close_pr')
      ),
      idempotency_key TEXT NOT NULL UNIQUE,
      intent_json TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('pending','in_flight','confirmed','failed','reconciling','abandoned')),
      attempts INTEGER NOT NULL DEFAULT 0,
      remote_ref TEXT,
      last_error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (phase_id, project_id) REFERENCES phases(id, project_id),
      FOREIGN KEY (candidate_id, project_id) REFERENCES candidates(id, project_id)
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_one_inflight_merge
      ON outbox_ops(project_id)
      WHERE op='merge_pr' AND status IN ('pending','in_flight','reconciling');
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      phase_id TEXT,
      state TEXT NOT NULL,
      snapshot_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY (task_id, project_id) REFERENCES tasks(id, project_id),
      FOREIGN KEY (phase_id, project_id) REFERENCES phases(id, project_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS transitions (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      entity_type TEXT NOT NULL CHECK (entity_type IN ('task','phase')),
      entity_id TEXT NOT NULL,
      from_state TEXT,
      to_state TEXT NOT NULL,
      reason TEXT NOT NULL,
      actor TEXT NOT NULL,
      candidate_id TEXT,
      policy_version INTEGER NOT NULL,
      underlying INTEGER NOT NULL DEFAULT 0,
      evidence_refs_json TEXT NOT NULL DEFAULT '[]',
      correlation_id TEXT NOT NULL,
      at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id TEXT PRIMARY KEY,
      project_id TEXT,
      actor TEXT NOT NULL,
      action TEXT NOT NULL,
      detail_json TEXT NOT NULL,
      correlation_id TEXT,
      at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_steps (
      step_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      entity_type TEXT NOT NULL CHECK (entity_type IN ('task','phase')),
      entity_id TEXT NOT NULL,
      thread_id TEXT NOT NULL,
      generation INTEGER NOT NULL,
      node TEXT NOT NULL,
      entry_version INTEGER NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('started','effects_settled','completed','abandoned')),
      candidate_id_in TEXT,
      candidate_id_out TEXT,
      result_ref TEXT,
      transition_id TEXT,
      abandon_reason TEXT,
      retry_disposition TEXT,
      preserve_reset_done INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      completed_at TEXT,
      CHECK (status <> 'completed' OR transition_id IS NOT NULL),
      CHECK (status <> 'abandoned' OR abandon_reason IS NOT NULL)
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_workflow_steps_non_abandoned
      ON workflow_steps(entity_id, node, entry_version)
      WHERE status <> 'abandoned';
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished_step
      ON workflow_steps(entity_id, node)
      WHERE status IN ('started','effects_settled');
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_metadata (
      backup_generation TEXT PRIMARY KEY,
      created_at TEXT NOT NULL
    );
    """,
    # Append-only triggers on transitions
    """
    CREATE TRIGGER IF NOT EXISTS trg_transitions_no_update
    BEFORE UPDATE ON transitions
    BEGIN
      SELECT RAISE(ABORT, 'transitions table is append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_transitions_no_delete
    BEFORE DELETE ON transitions
    BEGIN
      SELECT RAISE(ABORT, 'transitions table is append-only');
    END;
    """,
    # Append-only triggers on audit_log
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
      SELECT RAISE(ABORT, 'audit_log table is append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
    BEFORE DELETE ON audit_log
    BEGIN
      SELECT RAISE(ABORT, 'audit_log table is append-only');
    END;
    """,
    # Append-only triggers on agent_revisions
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_revisions_no_update
    BEFORE UPDATE ON agent_revisions
    BEGIN
      SELECT RAISE(ABORT, 'agent_revisions table is append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_revisions_no_delete
    BEFORE DELETE ON agent_revisions
    BEGIN
      SELECT RAISE(ABORT, 'agent_revisions table is append-only');
    END;
    """,
    # Trigger: phases.current_candidate_id must reference a candidates row with the same phase_id and project_id
    """
    CREATE TRIGGER IF NOT EXISTS trg_phases_current_candidate_insert
    BEFORE INSERT ON phases
    WHEN NEW.current_candidate_id IS NOT NULL
    BEGIN
      SELECT CASE
        WHEN NOT EXISTS (
          SELECT 1 FROM candidates c
          WHERE c.id = NEW.current_candidate_id
            AND c.phase_id = NEW.id
            AND c.project_id = NEW.project_id
        )
        THEN RAISE(ABORT, 'phases.current_candidate_id must belong to the same phase and project')
      END;
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_phases_current_candidate_update
    BEFORE UPDATE OF current_candidate_id ON phases
    WHEN NEW.current_candidate_id IS NOT NULL
    BEGIN
      SELECT CASE
        WHEN NOT EXISTS (
          SELECT 1 FROM candidates c
          WHERE c.id = NEW.current_candidate_id
            AND c.phase_id = NEW.id
            AND c.project_id = NEW.project_id
        )
        THEN RAISE(ABORT, 'phases.current_candidate_id must belong to the same phase and project')
      END;
    END;
    """,
)


def apply_schema_ddl(conn: sqlite3.Connection | Any) -> None:
    """Apply all canonical SQLite tables, indexes, and triggers."""
    cursor = conn.cursor()
    for statement in SCHEMA_Statements:
        cursor.execute(statement)
    conn.commit()
