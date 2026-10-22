"""Alembic migration runner for ATLAS SQLite database."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def get_alembic_config(db_path: Path) -> Config:
    """Create an Alembic Config pointing at the embedded migrations directory."""
    migrations_dir = Path(__file__).parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def run_migrations(db_path: Path) -> None:
    """Upgrade the SQLite database at db_path to the latest Alembic head."""
    cfg = get_alembic_config(db_path)
    command.upgrade(cfg, "head")
