"""reports feature: report_only role, report_access flag, report_documents

Revision ID: 0010_reports_feature
Revises: 0009_time_clock_pin_lookup
Create Date: 2026-06-09

Additive only:
- Widen the users.role CHECK constraint to allow 'report_only' (no existing row
  can violate a wider rule, so this is safe on live data).
- Add users.report_access boolean (defaults to False -> nobody gains access).
- Create the report_documents table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_reports_feature"
down_revision = "0009_time_clock_pin_lookup"
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
    # 1) Widen the role constraint to permit the new report_only role.
    _set_users_role_check(
        "role IN ('admin', 'manager', 'view_only', 'user', 'report_only')"
    )

    # 2) Add the report_access flag, defaulting to False for every existing user.
    op.add_column(
        "users",
        sa.Column(
            "report_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Drop the server_default now that existing rows are populated; the ORM
    # supplies the default for new rows.
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("report_access", server_default=None)

    # 3) Create the report_documents table.
    op.create_table(
        "report_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "report_key",
            sa.String(length=120),
            nullable=False,
            server_default="financial_explorer",
        ),
        sa.Column(
            "title",
            sa.String(length=255),
            nullable=False,
            server_default="Greystones Financial Explorer",
        ),
        sa.Column("html_content", sa.Text(), nullable=False),
        sa.Column("content_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("uploaded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_label", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('manual', 'auto')", name="ck_report_documents_source"),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_report_documents_report_key", "report_documents", ["report_key"]
    )
    op.create_index(
        "ix_report_documents_created_at", "report_documents", ["created_at"]
    )
    op.create_index(
        "ix_report_documents_uploaded_by_user_id",
        "report_documents",
        ["uploaded_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_report_documents_uploaded_by_user_id", table_name="report_documents")
    op.drop_index("ix_report_documents_created_at", table_name="report_documents")
    op.drop_index("ix_report_documents_report_key", table_name="report_documents")
    op.drop_table("report_documents")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("report_access")

    # Restore the previous (narrower) role constraint. Any report_only users
    # must be reassigned before downgrading, or this will fail by design.
    _set_users_role_check("role IN ('admin', 'manager', 'view_only', 'user')")
