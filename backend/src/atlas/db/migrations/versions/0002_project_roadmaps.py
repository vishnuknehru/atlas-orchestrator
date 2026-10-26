"""Project roadmaps: architect-authored phase plans stored as tasks.

Revision ID: 0002_project_roadmaps
Revises: 0001_initial_schema
Create Date: 2026-10-26
"""

from __future__ import annotations

from alembic import op

from atlas.db.models import ROADMAP_TABLE_STATEMENT, ROADMAP_TASK_COLUMNS

revision: str = "0002_project_roadmaps"
down_revision: str | None = "0001_initial_schema"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(ROADMAP_TABLE_STATEMENT)
    existing = {row[1] for row in bind.exec_driver_sql("PRAGMA table_info(tasks);").fetchall()}
    for column, ddl in ROADMAP_TASK_COLUMNS:
        if column not in existing:
            bind.exec_driver_sql(f"ALTER TABLE tasks ADD COLUMN {column} {ddl};")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_tasks_roadmap ON tasks(roadmap_id, roadmap_seq);")


def downgrade() -> None:
    raise RuntimeError("ATLAS schema migrations do not support destructive downgrades")
