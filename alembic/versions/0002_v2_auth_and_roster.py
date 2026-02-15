"""v2 auth and persistent roster

Revision ID: 0002_v2_auth_and_roster
Revises: 0001_initial
Create Date: 2026-02-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_v2_auth_and_roster"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("violations")
    op.drop_table("assignments")
    op.drop_table("time_off")
    op.drop_table("availability")
    op.drop_table("runs")
    op.drop_table("settings")
    op.drop_table("employees")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("min_hours_per_week", sa.Integer(), nullable=False),
        sa.Column("max_hours_per_week", sa.Integer(), nullable=False),
        sa.Column("priority_tier", sa.String(length=1), nullable=False),
        sa.Column("availability", sa.JSON(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('Store Clerk', 'Team Leader', 'Store Manager', 'Boat Captain')",
            name="ck_employees_role",
        ),
        sa.CheckConstraint("priority_tier IN ('A', 'B', 'C')", name="ck_employees_priority"),
        sa.UniqueConstraint("employee_id"),
    )
    op.create_index("ix_employees_employee_id", "employees", ["employee_id"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(length=128), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_employees_employee_id", table_name="employees")
    op.drop_table("employees")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("season", sa.String(20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("manager_consecutive_days_off", sa.Integer(), nullable=False),
    )
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("leadership_score", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
    )
    op.create_table(
        "availability",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("block", sa.String(20), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("employee_id", "day_of_week", "block", name="uq_employee_block"),
    )
    op.create_table(
        "time_off",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("note", sa.String(200), nullable=False),
        sa.UniqueConstraint("employee_id", "date", name="uq_employee_date_off"),
    )
    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("block", sa.String(20), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("run_id", "date", "block", name="uq_run_block"),
    )
    op.create_table(
        "violations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
    )
