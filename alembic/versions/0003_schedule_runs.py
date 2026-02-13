"""add schedule_runs table

Revision ID: 0003_schedule_runs
Revises: 0002_v2_auth_and_roster
Create Date: 2026-02-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_schedule_runs"
down_revision = "0002_v2_auth_and_roster"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("weeks", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_schedule_runs_created_at", "schedule_runs", ["created_at"], unique=False)
    op.create_index("ix_schedule_runs_created_by_user_id", "schedule_runs", ["created_by_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_schedule_runs_created_by_user_id", table_name="schedule_runs")
    op.drop_index("ix_schedule_runs_created_at", table_name="schedule_runs")
    op.drop_table("schedule_runs")
