"""Persist non-secret per-job model metadata.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evaluation_jobs",
        sa.Column("requested_model", sa.String(length=32), nullable=False, server_default="hy3"),
    )
    op.add_column(
        "evaluation_jobs",
        sa.Column(
            "requires_transient_credentials", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("evaluation_jobs", "requires_transient_credentials")
    op.drop_column("evaluation_jobs", "requested_model")
