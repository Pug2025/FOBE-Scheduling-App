"""add student flag to employees

Revision ID: 0004_employee_student_flag
Revises: 0003_schedule_runs
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_employee_student_flag"
down_revision = "0003_schedule_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("student", sa.Boolean(), nullable=False, server_default=sa.false()))
    # SQLite doesn't support "ALTER COLUMN ... DROP DEFAULT"; keep default in-place there.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("employees", "student", server_default=None)


def downgrade() -> None:
    op.drop_column("employees", "student")
