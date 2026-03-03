"""add pin lookup and temporary pin state

Revision ID: 0009_time_clock_pin_lookup
Revises: 0008_time_clock_prototype
Create Date: 2026-03-02
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_time_clock_pin_lookup"
down_revision = "0008_time_clock_prototype"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("clock_pin_lookup", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("clock_pin_temporary", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_users_clock_pin_lookup", "users", ["clock_pin_lookup"], unique=True)
    users = sa.table("users", sa.column("clock_pin_enabled", sa.Boolean()))
    op.execute(
        users.update()
        .where(users.c.clock_pin_enabled.is_(True))
        .values(clock_pin_enabled=False)
    )


def downgrade() -> None:
    op.drop_index("ix_users_clock_pin_lookup", table_name="users")
    op.drop_column("users", "clock_pin_temporary")
    op.drop_column("users", "clock_pin_lookup")
