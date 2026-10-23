"""SQLite WAL connection manager, single-writer coordinator, paired backups, and ownership validators."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from atlas.db.models import apply_schema_ddl
from atlas.utils import ensure_secure_dir, ensure_secure_file, new_ulid, utc_now_iso


class RepositoryIntegrityError(ValueError):
    """Raised when application-enforced composite ownership or DAG rules fail (ATLAS-014)."""


class PairedBackupError(RuntimeError):
    """Raised when paired atlas.db + graph.db backup or restore fails generation checks (ATLAS-L05)."""


def _configure_connection_pragmas(conn: sqlite3.Connection) -> None:
    """Apply mandatory SQLite pragmas on every connection (docs/database-design.md §1)."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")


class DatabaseManager:
    """Manages ~/.atlas/atlas.db and ~/.atlas/graph.db with single-writer serialization."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = ensure_secure_dir(data_dir.expanduser().resolve())
        self.atlas_db_path = self.data_dir / "atlas.db"
        self.graph_db_path = self.data_dir / "graph.db"
        self.backups_dir = ensure_secure_dir(self.data_dir / "backups")
        self.artifacts_dir = ensure_secure_dir(self.data_dir / "artifacts")
        self.logs_dir = ensure_secure_dir(self.data_dir / "logs")
        self.run_dir = ensure_secure_dir(self.data_dir / "run")
        self.projects_dir = ensure_secure_dir(self.data_dir / "projects")
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        """Create/migrate schema on atlas.db and ensure 0600 permissions on both DB files."""
        with self.connect(autocommit=False) as conn:
            apply_schema_ddl(conn)
        ensure_secure_file(self.atlas_db_path)

        # Ensure graph.db exists with 0600 permissions and backup_metadata table
        graph_conn = sqlite3.connect(
            str(self.graph_db_path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            _configure_connection_pragmas(graph_conn)
            graph_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_metadata (
                  backup_generation TEXT PRIMARY KEY,
                  created_at TEXT NOT NULL
                );
                """
            )
        finally:
            graph_conn.close()
        ensure_secure_file(self.graph_db_path)

    def connect(self, *, autocommit: bool = False) -> sqlite3.Connection:
        """Open a configured SQLite connection to atlas.db."""
        isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = (
            None if autocommit else "DEFERRED"
        )
        conn = sqlite3.connect(
            str(self.atlas_db_path),
            timeout=5.0,
            isolation_level=isolation_level,
            check_same_thread=False,
        )
        _configure_connection_pragmas(conn)
        ensure_secure_file(self.atlas_db_path)
        return conn

    @contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        """Context manager for read queries."""
        conn = self.connect(autocommit=True)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def begin_immediate(self) -> Iterator[sqlite3.Connection]:
        """Single-writer transaction using BEGIN IMMEDIATE (docs/database-design.md §1)."""
        with self._write_lock:
            conn = sqlite3.connect(
                str(self.atlas_db_path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            _configure_connection_pragmas(conn)
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise
            finally:
                conn.close()

    def verify_pragmas(self) -> dict[str, Any]:
        """Return active pragma values on a fresh connection for verification tests."""
        with self.read_conn() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
            return {
                "journal_mode": str(journal_mode).lower(),
                "synchronous": int(synchronous),
                "foreign_keys": int(foreign_keys),
                "busy_timeout": int(busy_timeout),
            }

    def run_quick_check(self) -> bool:
        """Run PRAGMA quick_check on both atlas.db and graph.db (recovery-strategy.md §3 step 1)."""
        for db_path in (self.atlas_db_path, self.graph_db_path):
            if not db_path.exists():
                continue
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                rows = conn.execute("PRAGMA quick_check;").fetchall()
                if not rows or any(r[0] != "ok" for r in rows):
                    return False
            except sqlite3.DatabaseError:
                return False
            finally:
                conn.close()
        return True

    def create_paired_backup(self, label: str = "manual") -> tuple[str, Path]:
        """Create a quiesced, paired backup of atlas.db and graph.db under one backup_generation (ATLAS-L05)."""
        with self._write_lock:
            generation_id = f"{label}-{new_ulid()}"
            now = utc_now_iso()
            target_dir = ensure_secure_dir(self.backups_dir / generation_id)
            atlas_backup_path = target_dir / "atlas.db"
            graph_backup_path = target_dir / "graph.db"

            # Record backup_generation in both live DBs before online backup
            with self.begin_immediate() as app_conn:
                app_conn.execute("DELETE FROM backup_metadata;")
                app_conn.execute(
                    "INSERT INTO backup_metadata(backup_generation, created_at) VALUES (?, ?);",
                    (generation_id, now),
                )

            graph_conn = sqlite3.connect(str(self.graph_db_path), timeout=5.0, isolation_level=None)
            try:
                _configure_connection_pragmas(graph_conn)
                graph_conn.execute("BEGIN IMMEDIATE;")
                graph_conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS backup_metadata (
                      backup_generation TEXT PRIMARY KEY,
                      created_at TEXT NOT NULL
                    );
                    """
                )
                graph_conn.execute("DELETE FROM backup_metadata;")
                graph_conn.execute(
                    "INSERT INTO backup_metadata(backup_generation, created_at) VALUES (?, ?);",
                    (generation_id, now),
                )
                graph_conn.execute("COMMIT;")
            finally:
                graph_conn.close()

            # Use SQLite online backup API
            src_app = sqlite3.connect(str(self.atlas_db_path))
            dst_app = sqlite3.connect(str(atlas_backup_path))
            try:
                src_app.backup(dst_app)
            finally:
                dst_app.close()
                src_app.close()
            ensure_secure_file(atlas_backup_path)

            src_graph = sqlite3.connect(str(self.graph_db_path))
            dst_graph = sqlite3.connect(str(graph_backup_path))
            try:
                src_graph.backup(dst_graph)
            finally:
                dst_graph.close()
                src_graph.close()
            ensure_secure_file(graph_backup_path)

            return generation_id, target_dir

    def restore_paired_backup(self, backup_dir: Path) -> str:
        """Restore paired atlas.db and graph.db after verifying matching backup_generation (ATLAS-L05)."""
        atlas_backup_path = backup_dir / "atlas.db"
        graph_backup_path = backup_dir / "graph.db"
        if not atlas_backup_path.exists() or not graph_backup_path.exists():
            raise PairedBackupError("Both atlas.db and graph.db must exist in the backup directory")

        def _read_gen(path: Path) -> str | None:
            conn = sqlite3.connect(str(path))
            try:
                row = conn.execute(
                    "SELECT backup_generation FROM backup_metadata LIMIT 1;"
                ).fetchone()
                return str(row[0]) if row else None
            except sqlite3.DatabaseError as exc:
                raise PairedBackupError(f"Failed to read backup_metadata from {path}: {exc}") from exc
            finally:
                conn.close()

        app_gen = _read_gen(atlas_backup_path)
        graph_gen = _read_gen(graph_backup_path)
        if not app_gen or not graph_gen or app_gen != graph_gen:
            raise PairedBackupError(
                f"Mismatched backup_generation: atlas.db={app_gen!r}, graph.db={graph_gen!r}"
            )

        with self._write_lock:
            src_app = sqlite3.connect(str(atlas_backup_path))
            dst_app = sqlite3.connect(str(self.atlas_db_path))
            try:
                src_app.backup(dst_app)
            finally:
                dst_app.close()
                src_app.close()
            ensure_secure_file(self.atlas_db_path)

            src_graph = sqlite3.connect(str(graph_backup_path))
            dst_graph = sqlite3.connect(str(self.graph_db_path))
            try:
                src_graph.backup(dst_graph)
            finally:
                dst_graph.close()
                src_graph.close()
            ensure_secure_file(self.graph_db_path)

        return app_gen


# ---------------------------------------------------------------------------
# Repository-layer integrity helpers for nullable composite FK paths (ATLAS-014)
# ---------------------------------------------------------------------------


def insert_execution_checked(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert an executions row with strict composite ownership checks for nullable columns."""
    project_id = row["project_id"]
    task_id = row["task_id"]
    phase_id = row.get("phase_id")
    candidate_id = row.get("candidate_id")

    # Verify task belongs to project
    task_row = conn.execute(
        "SELECT id FROM tasks WHERE id = ? AND project_id = ?;",
        (task_id, project_id),
    ).fetchone()
    if not task_row:
        raise RepositoryIntegrityError(
            f"Task {task_id!r} does not belong to project {project_id!r}"
        )

    if candidate_id is not None and phase_id is None:
        raise RepositoryIntegrityError(
            "An execution with candidate_id must also specify phase_id"
        )

    if phase_id is not None:
        phase_row = conn.execute(
            "SELECT id FROM phases WHERE id = ? AND task_id = ? AND project_id = ?;",
            (phase_id, task_id, project_id),
        ).fetchone()
        if not phase_row:
            raise RepositoryIntegrityError(
                f"Phase {phase_id!r} does not belong to task {task_id!r} / project {project_id!r}"
            )

    if candidate_id is not None:
        cand_row = conn.execute(
            "SELECT id FROM candidates WHERE id = ? AND phase_id = ? AND project_id = ?;",
            (candidate_id, phase_id, project_id),
        ).fetchone()
        if not cand_row:
            raise RepositoryIntegrityError(
                f"Candidate {candidate_id!r} does not belong to phase {phase_id!r} / project {project_id!r}"
            )

    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT INTO executions ({col_names}) VALUES ({placeholders});",
        tuple(row[c] for c in cols),
    )


def insert_review_round_checked(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert a review_rounds row with full composite ownership validation (ATLAS-014 r3)."""
    project_id = row["project_id"]
    subject_type = row["subject_type"]
    plan_id = row.get("plan_id")
    candidate_id = row.get("candidate_id")
    execution_id = row.get("execution_id")

    if subject_type == "plan":
        if not plan_id or candidate_id is not None:
            raise RepositoryIntegrityError(
                "Plan review_round requires plan_id and candidate_id=None"
            )
        plan_row = conn.execute(
            "SELECT task_id FROM plans WHERE id = ? AND project_id = ?;",
            (plan_id, project_id),
        ).fetchone()
        if not plan_row:
            raise RepositoryIntegrityError(
                f"Plan {plan_id!r} not found in project {project_id!r}"
            )
        if execution_id is not None:
            exec_row = conn.execute(
                "SELECT task_id, candidate_id FROM executions WHERE id = ? AND project_id = ?;",
                (execution_id, project_id),
            ).fetchone()
            if not exec_row:
                raise RepositoryIntegrityError(
                    f"Execution {execution_id!r} not found in project {project_id!r}"
                )
            if exec_row["task_id"] != plan_row["task_id"] or exec_row["candidate_id"] is not None:
                raise RepositoryIntegrityError(
                    "Plan review execution must belong to the same task as the plan and have candidate_id IS NULL"
                )
    elif subject_type == "candidate":
        if not candidate_id or plan_id is not None:
            raise RepositoryIntegrityError(
                "Candidate review_round requires candidate_id and plan_id=None"
            )
        cand_row = conn.execute(
            "SELECT phase_id FROM candidates WHERE id = ? AND project_id = ?;",
            (candidate_id, project_id),
        ).fetchone()
        if not cand_row:
            raise RepositoryIntegrityError(
                f"Candidate {candidate_id!r} not found in project {project_id!r}"
            )
        if execution_id is not None:
            exec_row = conn.execute(
                "SELECT candidate_id FROM executions WHERE id = ? AND project_id = ?;",
                (execution_id, project_id),
            ).fetchone()
            if not exec_row or exec_row["candidate_id"] != candidate_id:
                raise RepositoryIntegrityError(
                    f"Candidate review execution {execution_id!r} must belong to the exact same candidate {candidate_id!r}"
                )
    else:
        raise RepositoryIntegrityError(f"Invalid subject_type {subject_type!r}")

    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT INTO review_rounds ({col_names}) VALUES ({placeholders});",
        tuple(row[c] for c in cols),
    )


def validate_phase_dependencies(
    conn: sqlite3.Connection, project_id: str, task_id: str
) -> None:
    """Verify that all phase_dependencies in a task belong to the same task and form a DAG."""
    phases = conn.execute(
        "SELECT id FROM phases WHERE project_id = ? AND task_id = ?;",
        (project_id, task_id),
    ).fetchall()
    task_phase_ids = { str(r["id"]) for r in phases }

    deps = conn.execute(
        """
        SELECT pd.phase_id, pd.depends_on
        FROM phase_dependencies pd
        JOIN phases p ON p.id = pd.phase_id AND p.project_id = pd.project_id
        WHERE pd.project_id = ? AND p.task_id = ?;
        """,
        (project_id, task_id),
    ).fetchall()

    adjacency: dict[str, list[str]] = {pid: [] for pid in task_phase_ids}
    for row in deps:
        phase_id = str(row["phase_id"])
        depends_on = str(row["depends_on"])
        if depends_on not in task_phase_ids:
            raise RepositoryIntegrityError(
                f"Phase {phase_id!r} depends on phase {depends_on!r} outside task {task_id!r}"
            )
        adjacency[phase_id].append(depends_on)

    # Cycle detection via DFS coloring (0=unvisited, 1=visiting, 2=visited)
    state: dict[str, int] = {pid: 0 for pid in task_phase_ids}

    def _dfs(node: str) -> None:
        state[node] = 1
        for neighbor in adjacency.get(node, []):
            if state[neighbor] == 1:
                raise RepositoryIntegrityError(
                    f"Cycle detected in phase dependencies involving {node!r} -> {neighbor!r}"
                )
            if state[neighbor] == 0:
                _dfs(neighbor)
        state[node] = 2

    for pid in task_phase_ids:
        if state[pid] == 0:
            _dfs(pid)
