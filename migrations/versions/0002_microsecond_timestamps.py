"""Use microsecond precision for queue timestamps.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from alembic import op
from sqlalchemy.dialects import mysql


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


COLUMNS = {
    "available_at": False,
    "lease_expires_at": True,
    "heartbeat_at": True,
    "created_at": False,
    "updated_at": False,
    "started_at": True,
    "finished_at": True,
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    for column, nullable in COLUMNS.items():
        op.alter_column(
            "evaluation_jobs",
            column,
            existing_type=mysql.DATETIME(),
            type_=mysql.DATETIME(fsp=6),
            existing_nullable=nullable,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    for column, nullable in COLUMNS.items():
        op.alter_column(
            "evaluation_jobs",
            column,
            existing_type=mysql.DATETIME(fsp=6),
            type_=mysql.DATETIME(),
            existing_nullable=nullable,
        )
