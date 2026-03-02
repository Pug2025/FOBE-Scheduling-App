"""add time clock prototype tables and pin fields

Revision ID: 0008_time_clock_prototype
Revises: 0007_day_off_requests_links
Create Date: 2026-03-02
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_time_clock_prototype"
down_revision = "0007_day_off_requests_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("clock_pin_hash", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("clock_pin_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("clock_pin_updated_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "kiosk_sessions",
        sa.Column("session_id", sa.String(length=128), primary_key=True),
        sa.Column("unlocked_by_user_id", sa.Integer(), nullable=False),
        sa.Column("session_label", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["unlocked_by_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_kiosk_sessions_unlocked_by_user_id", "kiosk_sessions", ["unlocked_by_user_id"], unique=False)
    op.create_index("ix_kiosk_sessions_expires_at", "kiosk_sessions", ["expires_at"], unique=False)

    op.create_table(
        "attendance_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.String(length=120), nullable=False),
        sa.Column("employee_name_snapshot", sa.String(length=255), nullable=False),
        sa.Column("role_snapshot", sa.String(length=40), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("schedule_run_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_start_minutes", sa.Integer(), nullable=True),
        sa.Column("scheduled_end_minutes", sa.Integer(), nullable=True),
        sa.Column("scheduled_paid_minutes", sa.Integer(), nullable=True),
        sa.Column("actual_clock_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_clock_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_clock_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_clock_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_scheduled_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("break_deduction_minutes", sa.Integer(), nullable=True),
        sa.Column("payable_minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("review_state", sa.String(length=20), nullable=False, server_default="clear"),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("last_action_source", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_attendance_records_status"),
        sa.CheckConstraint("review_state IN ('clear', 'needs_review', 'approved')", name="ck_attendance_records_review_state"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["schedule_run_id"], ["schedule_runs.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_attendance_records_user_id", "attendance_records", ["user_id"], unique=False)
    op.create_index("ix_attendance_records_employee_id", "attendance_records", ["employee_id"], unique=False)
    op.create_index("ix_attendance_records_work_date", "attendance_records", ["work_date"], unique=False)
    op.create_index("ix_attendance_records_schedule_run_id", "attendance_records", ["schedule_run_id"], unique=False)
    op.create_index("ix_attendance_records_status", "attendance_records", ["status"], unique=False)
    op.create_index("ix_attendance_records_review_state", "attendance_records", ["review_state"], unique=False)
    op.create_index("ix_attendance_records_created_at", "attendance_records", ["created_at"], unique=False)

    op.create_table(
        "attendance_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attendance_record_id", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("previous_effective_clock_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_effective_clock_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_effective_clock_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_effective_clock_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('clock_in_override', 'clock_out_override', 'manager_edit', 'manager_approve')",
            name="ck_attendance_adjustments_action",
        ),
        sa.ForeignKeyConstraint(["attendance_record_id"], ["attendance_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_attendance_adjustments_attendance_record_id", "attendance_adjustments", ["attendance_record_id"], unique=False)
    op.create_index("ix_attendance_adjustments_requested_by_user_id", "attendance_adjustments", ["requested_by_user_id"], unique=False)
    op.create_index("ix_attendance_adjustments_created_at", "attendance_adjustments", ["created_at"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_attendance_adjustments_created_at", table_name="attendance_adjustments")
    op.drop_index("ix_attendance_adjustments_requested_by_user_id", table_name="attendance_adjustments")
    op.drop_index("ix_attendance_adjustments_attendance_record_id", table_name="attendance_adjustments")
    op.drop_table("attendance_adjustments")

    op.drop_index("ix_attendance_records_created_at", table_name="attendance_records")
    op.drop_index("ix_attendance_records_review_state", table_name="attendance_records")
    op.drop_index("ix_attendance_records_status", table_name="attendance_records")
    op.drop_index("ix_attendance_records_schedule_run_id", table_name="attendance_records")
    op.drop_index("ix_attendance_records_work_date", table_name="attendance_records")
    op.drop_index("ix_attendance_records_employee_id", table_name="attendance_records")
    op.drop_index("ix_attendance_records_user_id", table_name="attendance_records")
    op.drop_table("attendance_records")

    op.drop_index("ix_kiosk_sessions_expires_at", table_name="kiosk_sessions")
    op.drop_index("ix_kiosk_sessions_unlocked_by_user_id", table_name="kiosk_sessions")
    op.drop_table("kiosk_sessions")

    op.drop_column("users", "clock_pin_updated_at")
    op.drop_column("users", "clock_pin_enabled")
    op.drop_column("users", "clock_pin_hash")
