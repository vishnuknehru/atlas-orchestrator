"""Minimal safe restart, instance lock, and startup recovery sequence (Slice 1.6, recovery-strategy.md §3)."""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from atlas.config.schemas import (
    ADAPTER_MANIFESTS,
    AgentRegistryConfig,
    derive_provider,
    load_agent_registry,
)
from atlas.db.connection import DatabaseManager
from atlas.engine.workflow import LangGraphWorkflowEngine
from atlas.fsm.states import (
    PHASE_TERMINAL_STATES,
    TASK_TERMINAL_STATES,
    PhaseState,
    TaskState,
)
from atlas.fsm.transitions import (
    apply_phase_transition,
    apply_task_transition,
    release_repo_lock,
)
from atlas.utils import ensure_secure_dir, ensure_secure_file, new_ulid, utc_now_iso


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another ATLAS process already holds ~/.atlas/atlas.lock."""


class DatabaseIntegrityCheckError(RuntimeError):
    """Raised when PRAGMA quick_check fails on atlas.db or graph.db."""


class InstanceLock:
    """Exclusive flock on ~/.atlas/atlas.lock (recovery-strategy.md §3 step 2)."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = ensure_secure_dir(data_dir)
        self.lock_path = self.data_dir / "atlas.lock"
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        if self._fh is not None:
            return
        fh = open(self.lock_path, "a+", encoding="utf-8")
        ensure_secure_file(self.lock_path)
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise InstanceAlreadyRunningError(
                f"Another ATLAS instance is already running (lock held on {self.lock_path})"
            ) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    @property
    def is_locked(self) -> bool:
        return self._fh is not None


@dataclass
class RecoverySummary:
    ok: bool = True
    paused_tasks: list[str] = field(default_factory=list)
    paused_phases: list[str] = field(default_factory=list)
    resolved_steps: list[dict[str, Any]] = field(default_factory=list)
    diverged_entities: list[dict[str, str]] = field(default_factory=list)
    locks_released: list[str] = field(default_factory=list)
    completed_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sync_registry_to_db(db: DatabaseManager, registry: AgentRegistryConfig) -> None:
    """Synchronize ~/.atlas/agents.yaml into agents and append-only agent_revisions tables."""
    now = utc_now_iso()
    with db.begin_immediate() as conn:
        for agent_id, entry in registry.agents.items():
            manifest = ADAPTER_MANIFESTS[entry.adapter]
            provider = derive_provider(entry)
            models_json = json.dumps(entry.models)
            allow_remote_int = 1 if entry.allow_remote_endpoint else 0

            if entry.adapter == "manual_handoff":
                integration_mode = "manual_handoff"
                cap_state = "MANUAL_ONLY"
                auth_status = "manual"
            elif manifest.kind == "cli" and entry.billing == "api_key" and not manifest.credential_isolation_verified:
                integration_mode = "blocked"
                cap_state = "UNKNOWN"
                auth_status = "blocked_api_key_not_isolated"
            else:
                integration_mode = "headless"
                cap_state = "AVAILABLE"
                auth_status = "configured"

            existing = conn.execute(
                "SELECT id, current_revision FROM agents WHERE id = ?;",
                (agent_id,),
            ).fetchone()

            if not existing:
                rev_no = 1
                conn.execute(
                    """
                    INSERT INTO agents (
                      id, integration_mode, adapter, current_revision, cli_version,
                      help_hash, capabilities_json, auth_status, capacity_state,
                      capacity_reason, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?);
                    """,
                    (
                        agent_id,
                        integration_mode,
                        entry.adapter,
                        rev_no,
                        json.dumps({"allowed_roles": sorted(manifest.allowed_roles)}),
                        auth_status,
                        cap_state,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_revisions (
                      agent_id, revision, adapter, billing, provider, models_json,
                      model_family_declared, endpoint, allow_remote_endpoint, manifest_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?);
                    """,
                    (
                        agent_id,
                        rev_no,
                        entry.adapter,
                        entry.billing,
                        provider,
                        models_json,
                        entry.model_family,
                        entry.endpoint,
                        allow_remote_int,
                        now,
                    ),
                )
            else:
                cur_rev = int(existing["current_revision"])
                prev_rev = conn.execute(
                    "SELECT * FROM agent_revisions WHERE agent_id = ? AND revision = ?;",
                    (agent_id, cur_rev),
                ).fetchone()
                changed = (
                    not prev_rev
                    or prev_rev["adapter"] != entry.adapter
                    or prev_rev["billing"] != entry.billing
                    or prev_rev["provider"] != provider
                    or prev_rev["models_json"] != models_json
                    or prev_rev["model_family_declared"] != entry.model_family
                    or prev_rev["endpoint"] != entry.endpoint
                    or int(prev_rev["allow_remote_endpoint"]) != allow_remote_int
                )
                if changed:
                    new_rev = cur_rev + 1
                    conn.execute(
                        """
                        INSERT INTO agent_revisions (
                          agent_id, revision, adapter, billing, provider, models_json,
                          model_family_declared, endpoint, allow_remote_endpoint, manifest_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?);
                        """,
                        (
                            agent_id,
                            new_rev,
                            entry.adapter,
                            entry.billing,
                            provider,
                            models_json,
                            entry.model_family,
                            entry.endpoint,
                            allow_remote_int,
                            now,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE agents
                        SET integration_mode = ?, adapter = ?, current_revision = ?,
                            auth_status = ?, capacity_state = ?, updated_at = ?
                        WHERE id = ?;
                        """,
                        (
                            integration_mode,
                            entry.adapter,
                            new_rev,
                            auth_status,
                            cap_state,
                            now,
                            agent_id,
                        ),
                    )


def run_startup_recovery(
    db: DatabaseManager,
    engine: LangGraphWorkflowEngine,
) -> RecoverySummary:
    """Execute the Phase 1 safe-restart recovery sequence (recovery-strategy.md §3)."""
    summary = RecoverySummary()

    # Step 1: PRAGMA quick_check
    if not db.run_quick_check():
        summary.ok = False
        raise DatabaseIntegrityCheckError(
            "SQLite PRAGMA quick_check failed on atlas.db or graph.db; refusing to start scheduler"
        )

    # Sync agent registry into DB
    registry = load_agent_registry(db.data_dir)
    sync_registry_to_db(db, registry)

    # Step 3 & 8: Mark any leftover launching/running executions as interrupted
    now = utc_now_iso()
    with db.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE executions
            SET status = 'interrupted', finished_at = ?
            WHERE status IN ('launching', 'running');
            """,
            (now,),
        )

    # Step 11a: Resolve unfinished workflow steps BEFORE any PAUSED overlay (ADR-006 §5.1)
    summary.resolved_steps = engine.resolve_unfinished_steps()

    # Step 12: Mark every non-terminal, non-paused, non-blocked task and phase as PAUSED (auto_resume=False)
    terminal_task_vals = tuple(s.value for s in TASK_TERMINAL_STATES)
    terminal_phase_vals = tuple(s.value for s in PHASE_TERMINAL_STATES)

    with db.begin_immediate() as conn:
        t_placeholders = ", ".join("?" for _ in terminal_task_vals)
        active_tasks = conn.execute(
            f"""
            SELECT id, state FROM tasks
            WHERE state NOT IN ({t_placeholders}, 'PAUSED', 'BLOCKED');
            """,
            terminal_task_vals,
        ).fetchall()

        for t_row in active_tasks:
            t_id = str(t_row["id"])
            apply_task_transition(
                conn,
                task_id=t_id,
                to_state=TaskState.PAUSED.value,
                reason="Minimal safe restart: paused on startup (auto_resume=false)",
                actor="recovery",
            )
            summary.paused_tasks.append(t_id)

        p_placeholders = ", ".join("?" for _ in terminal_phase_vals)
        active_phases = conn.execute(
            f"""
            SELECT id, state, remote_merge_withdrawn FROM phases
            WHERE state NOT IN ({p_placeholders}, 'PAUSED', 'BLOCKED');
            """,
            terminal_phase_vals,
        ).fetchall()

        for p_row in active_phases:
            p_id = str(p_row["id"])
            p_state = str(p_row["state"])
            if p_state in (
                PhaseState.MERGE_PENDING_REMOTE.value,
                PhaseState.CANCELLING_REMOTE_MERGE.value,
            ) and not int(p_row["remote_merge_withdrawn"] or 0):
                apply_phase_transition(
                    conn,
                    phase_id=p_id,
                    to_state=PhaseState.BLOCKED.value,
                    reason="Remote merge withdrawal not confirmed during startup",
                    actor="recovery",
                    blocked_reason="remote_merge_not_withdrawn",
                )
            else:
                apply_phase_transition(
                    conn,
                    phase_id=p_id,
                    to_state=PhaseState.PAUSED.value,
                    reason="Minimal safe restart: paused on startup (auto_resume=false)",
                    actor="recovery",
                )
                summary.paused_phases.append(p_id)

        # Step 12a: Release repo_locks held by dead instances only after phase is PAUSED/BLOCKED/terminal
        locks = conn.execute("SELECT project_id, phase_id FROM repo_locks;").fetchall()
        for lk in locks:
            proj_id = str(lk["project_id"])
            ph_id = str(lk["phase_id"])
            ph_row = conn.execute(
                "SELECT state, blocked_reason FROM phases WHERE id = ?;",
                (ph_id,),
            ).fetchone()
            if ph_row and ph_row["blocked_reason"] == "remote_merge_not_withdrawn":
                continue
            if release_repo_lock(conn, project_id=proj_id, phase_id=ph_id):
                summary.locks_released.append(proj_id)

    # Step 13: LangGraph thread classification (ADR-006 §5.2)
    with db.read_conn() as rconn:
        all_tasks = rconn.execute(
            f"SELECT id FROM tasks WHERE state NOT IN ({t_placeholders});",
            terminal_task_vals,
        ).fetchall()
        all_phases = rconn.execute(
            f"SELECT id FROM phases WHERE state NOT IN ({p_placeholders});",
            terminal_phase_vals,
        ).fetchall()

    for t_row in all_tasks:
        t_id = str(t_row["id"])
        cls_res = engine.classify_thread("task", t_id)
        if cls_res.status == "incompatible":
            summary.diverged_entities.append(
                {"entity_type": "task", "entity_id": t_id, "reason": cls_res.reason}
            )

    for p_row in all_phases:
        p_id = str(p_row["id"])
        cls_res = engine.classify_thread("phase", p_id)
        if cls_res.status == "incompatible":
            summary.diverged_entities.append(
                {"entity_type": "phase", "entity_id": p_id, "reason": cls_res.reason}
            )

    # Step 14: Audit recovery_completed
    summary.completed_at = utc_now_iso()
    with db.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (id, project_id, actor, action, detail_json, correlation_id, at)
            VALUES (?, NULL, 'recovery', 'recovery_completed', ?, ?, ?);
            """,
            (
                new_ulid(),
                json.dumps(summary.to_dict()),
                new_ulid(),
                summary.completed_at,
            ),
        )

    return summary
