"""track users who must change temporary passwords

Revision ID: 0006_user_temp_password_flag
Revises: 0005_expand_user_roles
Create Date: 2026-02-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_user_temp_password_flag"
down_revision = "0005_expand_user_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
    # SQLite doesn't support "ALTER COLUMN ... DROP DEFAULT"; keep default in-place there.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("users", "must_change_password", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
