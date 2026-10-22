"""Baseline Phase 1 schema migration.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-22
"""

from __future__ import annotations

from alembic import op

from atlas.db.models import SCHEMA_Statements

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    for statement in SCHEMA_Statements:
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    raise RuntimeError("ATLAS schema migrations do not support destructive downgrades")
