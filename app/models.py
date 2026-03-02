from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'manager', 'view_only', 'user')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    linked_employee_id: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True, index=True)
    clock_pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    clock_pin_lookup: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    clock_pin_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    clock_pin_temporary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    clock_pin_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    sessions = relationship("SessionRecord", back_populates="user", cascade="all, delete-orphan")
    kiosk_sessions = relationship("KioskSession", back_populates="unlocked_by", cascade="all, delete-orphan")
    schedule_runs = relationship("ScheduleRun", back_populates="created_by", cascade="all, delete-orphan")
    day_off_requests = relationship("DayOffRequest", back_populates="requester", foreign_keys="DayOffRequest.requester_user_id", cascade="all, delete-orphan")
    attendance_records = relationship("AttendanceRecord", back_populates="user", cascade="all, delete-orphan")
    attendance_adjustments = relationship("AttendanceAdjustment", back_populates="requested_by")


class EmployeeRecord(Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint(
            "role IN ('Store Clerk', 'Team Leader', 'Store Manager', 'Boat Captain')",
            name="ck_employees_role",
        ),
        CheckConstraint("priority_tier IN ('A', 'B', 'C')", name="ck_employees_priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    min_hours_per_week: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hours_per_week: Mapped[int] = mapped_column(Integer, nullable=False)
    priority_tier: Mapped[str] = mapped_column(String(1), nullable=False, default="B")
    student: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    availability: Mapped[dict[str, list[str]]] = mapped_column(JSON, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class SessionRecord(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    user = relationship("User", back_populates="sessions")


class KioskSession(Base):
    __tablename__ = "kiosk_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    unlocked_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    unlocked_by = relationship("User", back_populates="kiosk_sessions")


class ScheduleRun(Base):
    __tablename__ = "schedule_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_by = relationship("User", back_populates="schedule_runs")


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="ck_attendance_records_status"),
        CheckConstraint("review_state IN ('clear', 'needs_review', 'approved')", name="ck_attendance_records_review_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    employee_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    role_snapshot: Mapped[str | None] = mapped_column(String(40), nullable=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    schedule_run_id: Mapped[int | None] = mapped_column(ForeignKey("schedule_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    scheduled_start_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_end_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_paid_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_clock_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_clock_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_scheduled_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    break_deduction_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payable_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    review_state: Mapped[str] = mapped_column(String(20), nullable=False, default="clear", index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_action_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="attendance_records")
    adjustments = relationship("AttendanceAdjustment", back_populates="attendance_record", cascade="all, delete-orphan")


class AttendanceAdjustment(Base):
    __tablename__ = "attendance_adjustments"
    __table_args__ = (
        CheckConstraint(
            "action IN ('clock_in_override', 'clock_out_override', 'manager_edit', 'manager_approve')",
            name="ck_attendance_adjustments_action",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attendance_record_id: Mapped[int] = mapped_column(ForeignKey("attendance_records.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_effective_clock_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_effective_clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_effective_clock_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_effective_clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    attendance_record = relationship("AttendanceRecord", back_populates="adjustments")
    requested_by = relationship("User", back_populates="attendance_adjustments")


class DayOffRequest(Base):
    __tablename__ = "day_off_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')", name="ck_day_off_requests_status"),
        CheckConstraint("start_date <= end_date", name="ck_day_off_requests_date_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    requester_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    request_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    cancelled_by_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    requester = relationship("User", foreign_keys=[requester_user_id], back_populates="day_off_requests")
