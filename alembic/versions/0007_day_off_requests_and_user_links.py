"""add user employee link and day-off request workflow tables

Revision ID: 0007_day_off_requests_and_user_links
Revises: 0006_user_temp_password_flag
Create Date: 2026-02-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_day_off_requests_and_user_links"
down_revision = "0006_user_temp_password_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("linked_employee_id", sa.String(length=120), nullable=True))
    op.create_index("ix_users_linked_employee_id", "users", ["linked_employee_id"], unique=True)

    op.create_table(
        "day_off_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requester_user_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.String(length=120), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("request_reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Integer(), nullable=True),
        sa.Column("cancelled_by_role", sa.String(length=20), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')", name="ck_day_off_requests_status"),
        sa.CheckConstraint("start_date <= end_date", name="ck_day_off_requests_date_range"),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cancelled_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_day_off_requests_requester_user_id", "day_off_requests", ["requester_user_id"], unique=False)
    op.create_index("ix_day_off_requests_employee_id", "day_off_requests", ["employee_id"], unique=False)
    op.create_index("ix_day_off_requests_start_date", "day_off_requests", ["start_date"], unique=False)
    op.create_index("ix_day_off_requests_end_date", "day_off_requests", ["end_date"], unique=False)
    op.create_index("ix_day_off_requests_status", "day_off_requests", ["status"], unique=False)
    op.create_index("ix_day_off_requests_decided_by_user_id", "day_off_requests", ["decided_by_user_id"], unique=False)
    op.create_index("ix_day_off_requests_cancelled_by_user_id", "day_off_requests", ["cancelled_by_user_id"], unique=False)
    op.create_index("ix_day_off_requests_created_at", "day_off_requests", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_day_off_requests_created_at", table_name="day_off_requests")
    op.drop_index("ix_day_off_requests_cancelled_by_user_id", table_name="day_off_requests")
    op.drop_index("ix_day_off_requests_decided_by_user_id", table_name="day_off_requests")
    op.drop_index("ix_day_off_requests_status", table_name="day_off_requests")
    op.drop_index("ix_day_off_requests_end_date", table_name="day_off_requests")
    op.drop_index("ix_day_off_requests_start_date", table_name="day_off_requests")
    op.drop_index("ix_day_off_requests_employee_id", table_name="day_off_requests")
    op.drop_index("ix_day_off_requests_requester_user_id", table_name="day_off_requests")
    op.drop_table("day_off_requests")

    op.drop_index("ix_users_linked_employee_id", table_name="users")
    op.drop_column("users", "linked_employee_id")
