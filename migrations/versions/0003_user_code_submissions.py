"""Persist user code submissions for restart-safe evaluation jobs.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evaluation_jobs", sa.Column("submission_json", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluation_jobs", "submission_json")
