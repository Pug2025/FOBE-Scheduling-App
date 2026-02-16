"""expand user roles for manager and view-only

Revision ID: 0005_expand_user_roles
Revises: 0004_employee_student_flag
Create Date: 2026-02-16
"""

from alembic import op

revision = "0005_expand_user_roles"
down_revision = "0004_employee_student_flag"
branch_labels = None
depends_on = None


def _set_users_role_check(check_sql: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table("users", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_users_role", type_="check")
            batch_op.create_check_constraint("ck_users_role", check_sql)
        return

    if dialect == "postgresql":
        op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    else:
        op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint("ck_users_role", "users", check_sql)


def upgrade() -> None:
    _set_users_role_check("role IN ('admin', 'manager', 'view_only', 'user')")


def downgrade() -> None:
    _set_users_role_check("role IN ('admin', 'user')")
