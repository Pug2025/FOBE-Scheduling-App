"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-02-12
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("settings", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("season", sa.String(20), nullable=False), sa.Column("start_date", sa.Date(), nullable=False), sa.Column("horizon_days", sa.Integer(), nullable=False), sa.Column("manager_consecutive_days_off", sa.Integer(), nullable=False))
    op.create_table("employees", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(100), nullable=False, unique=True), sa.Column("role", sa.String(20), nullable=False), sa.Column("leadership_score", sa.Float(), nullable=False), sa.Column("active", sa.Boolean(), nullable=False))
    op.create_table("runs", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("seed", sa.Integer(), nullable=False))
    op.create_table("availability", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False), sa.Column("day_of_week", sa.Integer(), nullable=False), sa.Column("block", sa.String(20), nullable=False), sa.Column("available", sa.Boolean(), nullable=False), sa.UniqueConstraint("employee_id", "day_of_week", "block", name="uq_employee_block"))
    op.create_table("time_off", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False), sa.Column("date", sa.Date(), nullable=False), sa.Column("note", sa.String(200), nullable=False), sa.UniqueConstraint("employee_id", "date", name="uq_employee_date_off"))
    op.create_table("assignments", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False), sa.Column("date", sa.Date(), nullable=False), sa.Column("block", sa.String(20), nullable=False), sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False), sa.Column("locked", sa.Boolean(), nullable=False), sa.UniqueConstraint("run_id", "date", "block", name="uq_run_block"))
    op.create_table("violations", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False), sa.Column("date", sa.Date(), nullable=False), sa.Column("severity", sa.String(20), nullable=False), sa.Column("message", sa.Text(), nullable=False))


def downgrade() -> None:
    op.drop_table("violations")
    op.drop_table("assignments")
    op.drop_table("time_off")
    op.drop_table("availability")
    op.drop_table("runs")
    op.drop_table("employees")
    op.drop_table("settings")
