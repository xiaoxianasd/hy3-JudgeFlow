"""Create durable evaluation job queue.

Revision ID: 0001
Revises:
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("problem_id", sa.String(length=128), nullable=False),
        sa.Column("hypothesis_examples", sa.Integer(), nullable=False),
        sa.Column("review_mode", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_evaluation_jobs_status", "evaluation_jobs", ["status"])
    op.create_index("ix_evaluation_jobs_problem_id", "evaluation_jobs", ["problem_id"])
    op.create_index("ix_evaluation_jobs_lease_expires_at", "evaluation_jobs", ["lease_expires_at"])
    op.create_index("ix_evaluation_jobs_finished_at", "evaluation_jobs", ["finished_at"])
    op.create_index(
        "ix_evaluation_jobs_claim",
        "evaluation_jobs",
        ["status", "available_at", "priority", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("evaluation_jobs")
