from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AttendanceAdjustment,
    AttendanceRecord,
    DayOffRequest,
    EmployeeRecord,
    KioskSession,
    ScheduleRun,
    SessionRecord,
    User,
)
from app.security import hash_password, verify_password
from app.timeclock import (
    ALLOWED_EARLY_START_ROLES,
    AUTO_APPROVE_ADJUSTMENT_MINUTES,
    BREAK_POLICY_BANDS,
    CAPTAIN_ROLE,
    CAPTAIN_STANDARD_END_MINUTES,
    GRACE_MINUTES,
    LONG_SHIFT_REVIEW_THRESHOLD_MINUTES,
    MAX_SELF_SERVICE_ADJUSTMENT_MINUTES,
    WORKPLACE_TIMEZONE_NAME,
    break_policy_for_span,
    build_local_datetime,
    calculate_attendance_minutes,
    captain_shift_is_full_day,
    format_hours_from_minutes,
    format_local_time,
    format_minutes_as_clock,
    local_datetime_to_utc,
    local_now,
    normalize_captain_clock_in,
    normalize_captain_clock_out,
    now_utc,
    pin_lookup_key,
    parse_time_string,
    payable_minutes_for_span,
    scheduled_paid_minutes,
    span_minutes,
    utc_to_local,
)

app = FastAPI(title="FOBE Scheduler Prototype")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SESSION_COOKIE_NAME = "session_id"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
KIOSK_SESSION_COOKIE_NAME = "kiosk_session_id"
KIOSK_SESSION_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
MIN_DAYS_OFF_REQUEST_NOTICE_DAYS = 14
MANAGER_OR_ADMIN_ROLES = {"admin", "manager"}
DAY_OFF_STATUS_VALUES = {"pending", "approved", "rejected", "cancelled"}

UserRoleInput = Literal["admin", "manager", "view_only", "user"]
UserRole = Literal["admin", "manager", "view_only"]


@app.middleware("http")
async def disable_cache_for_auth_and_api(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/auth/") or path in {"/kiosk", "/time-clock"}:
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


class AuthPayload(BaseModel):
    email: str
    password: str


class PasswordChangePayload(BaseModel):
    current_password: str
    new_password: str


class UserCreatePayload(BaseModel):
    email: str
    temporary_password: str
    role: UserRoleInput = "view_only"
    linked_employee_id: str | None = None


class UserPatchPayload(BaseModel):
    role: UserRoleInput | None = None
    temporary_password: str | None = None
    is_active: bool | None = None
    linked_employee_id: str | None = None


class UserOut(BaseModel):
    id: int
    email: str
    role: UserRole
    linked_employee_id: str | None = None
    is_active: bool
    must_change_password: bool
    created_at: datetime

    @classmethod
    def from_orm_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            role=canonicalize_user_role(user.role),
            linked_employee_id=user.linked_employee_id,
            is_active=user.is_active,
            must_change_password=user.must_change_password,
            created_at=user.created_at,
        )


DayOffRequestStatus = Literal["pending", "approved", "rejected", "cancelled"]


class DayOffRequestCreatePayload(BaseModel):
    start_date: date
    end_date: date
    reason: str = ""

    @model_validator(mode="after")
    def validate_date_range(self) -> "DayOffRequestCreatePayload":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class DayOffRequestCancelPayload(BaseModel):
    reason: str = ""


class DayOffRequestDecisionPayload(BaseModel):
    action: Literal["approve", "reject"]
    reason: str = ""


class DayOffRequestAdminCancelPayload(BaseModel):
    reason: str


class DayOffRequestOut(BaseModel):
    id: int
    requester_user_id: int
    requester_email: str | None = None
    employee_id: str
    employee_name: str | None = None
    start_date: date
    end_date: date
    reason: str
    status: DayOffRequestStatus
    decision_reason: str | None = None
    cancelled_reason: str | None = None
    cancelled_by_role: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    cancelled_at: datetime | None = None
    locked_by_schedule: bool = False


class ApprovedDayOffEntryOut(BaseModel):
    request_id: int
    employee_id: str
    employee_name: str | None = None
    date: date
    reason: str = ""


class ScheduleSavePayload(BaseModel):
    period_start: date
    weeks: int = Field(ge=1)
    label: str | None = None
    payload_json: dict[str, Any]
    result_json: dict[str, Any]


class ScheduleRunMetaOut(BaseModel):
    id: int
    created_at: datetime
    created_by_email: str
    period_start: date
    weeks: int
    label: str | None = None


class ScheduleRunOut(ScheduleRunMetaOut):
    payload_json: dict[str, Any]
    result_json: dict[str, Any]
    day_off_requests: list[DayOffRequestOut] = Field(default_factory=list)


class BootstrapStatusOut(BaseModel):
    enabled: bool


TimeClockReviewState = Literal["clear", "needs_review", "approved"]


class ClockPinPayload(BaseModel):
    pin: str
    temporary: bool = True


class KioskUnlockPayload(BaseModel):
    email: str
    password: str
    session_label: str | None = None


class KioskClockPayload(BaseModel):
    pin: str
    override_time: str | None = None
    override_reason: str = ""
    new_pin: str | None = None
    confirm_new_pin: str | None = None


class KioskLockPayload(BaseModel):
    email: str
    password: str


class KioskStatusOut(BaseModel):
    unlocked: bool
    unlocked_by_email: str | None = None
    expires_at: datetime | None = None
    session_label: str | None = None


class TimeClockStaffOut(BaseModel):
    user_id: int | None = None
    email: str | None = None
    account_role: UserRole | None = None
    linked_employee_id: str
    employee_name: str | None = None
    employee_role: str | None = None
    pin_enabled: bool
    pin_temporary: bool
    pin_status: Literal["not_set", "temporary", "active"]
    pin_updated_at: datetime | None = None
    is_active: bool
    has_linked_account: bool


class AttendanceRecordOut(BaseModel):
    id: int
    user_id: int
    employee_id: str
    employee_name: str
    role: str | None = None
    work_date: date
    scheduled_start: str | None = None
    scheduled_end: str | None = None
    scheduled_paid_hours: float | None = None
    actual_clock_in_local: str | None = None
    actual_clock_out_local: str | None = None
    effective_clock_in_local: str | None = None
    effective_clock_out_local: str | None = None
    break_deduction_minutes: int | None = None
    payable_hours: float | None = None
    status: Literal["open", "closed"]
    review_state: TimeClockReviewState
    review_note: str | None = None
    used_scheduled_default: bool
    last_action_source: str | None = None
    updated_at: datetime


class KioskClockResponse(BaseModel):
    action: Literal["clocked_in", "clocked_out", "pin_change_required"]
    message: str
    record: AttendanceRecordOut | None = None
    employee_name: str | None = None


class TimesheetRowOut(BaseModel):
    employee_id: str
    employee_name: str
    role: str | None = None
    work_date: date
    first_in_local: str | None = None
    last_out_local: str | None = None
    payable_hours: float
    break_deduction_minutes: int
    shift_count: int
    exception_count: int


class TimesheetEmployeeTotalOut(BaseModel):
    employee_id: str
    employee_name: str
    role: str | None = None
    payable_hours: float
    worked_days: int


class TimesheetOut(BaseModel):
    start_date: date
    end_date: date
    rows: list[TimesheetRowOut]
    employee_totals: list[TimesheetEmployeeTotalOut]
    grand_total_hours: float


class AttendanceRecordPatchPayload(BaseModel):
    effective_clock_in_local: str | None = None
    effective_clock_out_local: str | None = None
    reason: str = ""
    mark_review_state: TimeClockReviewState | None = None


class AttendanceApprovePayload(BaseModel):
    note: str = ""


class TimeClockPolicyBandOut(BaseModel):
    label: str
    min_span: str
    max_span: str | None = None
    deduction_minutes: int
    requires_review: bool = False


class TimeClockPolicyOut(BaseModel):
    timezone: str
    grace_minutes: int
    auto_approve_adjustment_minutes: int
    self_service_adjustment_limit_minutes: int
    long_shift_review_after_minutes: int
    allowed_early_start_roles: list[str]
    bands: list[TimeClockPolicyBandOut]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def canonicalize_user_role(raw_role: str) -> UserRole:
    if raw_role == "admin":
        return "admin"
    if raw_role == "manager":
        return "manager"
    if raw_role in {"view_only", "user"}:
        return "view_only"
    return "view_only"


def normalize_user_role_input(role: UserRoleInput) -> UserRole:
    if role == "user":
        return "view_only"
    return role


def ensure_password_strength(password: str) -> None:
    if len(password) < 10:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 10 characters")


def request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        first_proto = forwarded_proto.split(",")[0].strip().lower()
        if first_proto:
            return first_proto == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )


def create_session(db: Session, user_id: int) -> str:
    while True:
        session_id = secrets.token_urlsafe(32)
        existing = db.get(SessionRecord, session_id)
        if existing is None:
            break
    db.add(
        SessionRecord(
            session_id=session_id,
            user_id=user_id,
            expires_at=utcnow() + timedelta(days=14),
        )
    )
    db.commit()
    return session_id


def delete_session_if_exists(db: Session, session_id: str) -> None:
    session = db.get(SessionRecord, session_id)
    if session is not None:
        db.delete(session)
        db.commit()


def get_session_user(db: Session, session_id: str | None) -> User | None:
    if not session_id:
        return None
    session = db.get(SessionRecord, session_id)
    if session is None:
        return None
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utcnow():
        db.delete(session)
        db.commit()
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        db.delete(session)
        db.commit()
        return None
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = get_session_user(db, session_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def ensure_password_change_completed(user: User) -> None:
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required before accessing workspace data",
        )


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    ensure_password_change_completed(current_user)
    if canonicalize_user_role(current_user.role) != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def get_manager_or_admin_user(current_user: User = Depends(get_current_user)) -> User:
    ensure_password_change_completed(current_user)
    if canonicalize_user_role(current_user.role) not in MANAGER_OR_ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manager or admin access required")
    return current_user


def get_view_only_user(current_user: User = Depends(get_current_user)) -> User:
    ensure_password_change_completed(current_user)
    if canonicalize_user_role(current_user.role) != "view_only":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="View-only access required")
    return current_user


def get_requesting_user(current_user: User = Depends(get_current_user)) -> User:
    ensure_password_change_completed(current_user)
    role = canonicalize_user_role(current_user.role)
    if role not in {"manager", "view_only"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manager or view-only access required")
    return current_user


def normalize_linked_employee_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def ensure_linked_employee_exists(db: Session, linked_employee_id: str | None) -> None:
    if linked_employee_id is None:
        return
    employee = db.scalar(select(EmployeeRecord.id).where(EmployeeRecord.employee_id == linked_employee_id))
    if employee is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Linked employee was not found in roster")


def ensure_user_has_linked_employee(current_user: User, db: Session) -> str:
    linked_employee_id = normalize_linked_employee_id(current_user.linked_employee_id)
    if linked_employee_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your account is not linked to a roster employee. Ask an administrator to link your account.",
        )
    ensure_linked_employee_exists(db, linked_employee_id)
    return linked_employee_id


def ensure_clock_pin_strength(pin: str) -> str:
    normalized = (pin or "").strip()
    if len(normalized) < 4 or len(normalized) > 6 or not normalized.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PIN must be 4 to 6 digits")
    return normalized


def _clock_pin_state(user: User) -> Literal["not_set", "temporary", "active"]:
    if not user.clock_pin_enabled or not user.clock_pin_hash or not user.clock_pin_lookup:
        return "not_set"
    if user.clock_pin_temporary:
        return "temporary"
    return "active"


def _set_user_clock_pin(user: User, pin: str, *, temporary: bool) -> None:
    normalized_pin = ensure_clock_pin_strength(pin)
    user.clock_pin_hash = hash_password(normalized_pin)
    user.clock_pin_lookup = pin_lookup_key(normalized_pin)
    user.clock_pin_enabled = True
    user.clock_pin_temporary = temporary
    user.clock_pin_updated_at = utcnow()


def set_kiosk_session_cookie(response: Response, request: Request, session_id: str) -> None:
    response.set_cookie(
        key=KIOSK_SESSION_COOKIE_NAME,
        value=session_id,
        max_age=KIOSK_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )


def clear_kiosk_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=KIOSK_SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
    )


def create_kiosk_session(db: Session, user_id: int, session_label: str | None = None) -> str:
    while True:
        session_id = secrets.token_urlsafe(32)
        existing = db.get(KioskSession, session_id)
        if existing is None:
            break
    db.add(
        KioskSession(
            session_id=session_id,
            unlocked_by_user_id=user_id,
            session_label=(session_label or "").strip() or None,
            expires_at=utcnow() + timedelta(seconds=KIOSK_SESSION_MAX_AGE_SECONDS),
        )
    )
    db.commit()
    return session_id


def delete_kiosk_session_if_exists(db: Session, session_id: str | None) -> None:
    if not session_id:
        return
    session = db.get(KioskSession, session_id)
    if session is not None:
        db.delete(session)
        db.commit()


def get_kiosk_session_state(db: Session, session_id: str | None) -> tuple[KioskSession | None, User | None]:
    if not session_id:
        return (None, None)
    session = db.get(KioskSession, session_id)
    if session is None:
        return (None, None)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utcnow():
        db.delete(session)
        db.commit()
        return (None, None)
    user = db.get(User, session.unlocked_by_user_id)
    if user is None or not user.is_active or canonicalize_user_role(user.role) not in MANAGER_OR_ADMIN_ROLES:
        db.delete(session)
        db.commit()
        return (None, None)
    return (session, user)


def get_active_kiosk_session(request: Request, db: Session) -> tuple[KioskSession, User]:
    session_id = request.cookies.get(KIOSK_SESSION_COOKIE_NAME)
    session, user = get_kiosk_session_state(db, session_id)
    if session is None or user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kiosk is locked")
    session.last_used_at = utcnow()
    session.expires_at = utcnow() + timedelta(seconds=KIOSK_SESSION_MAX_AGE_SECONDS)
    db.add(session)
    db.commit()
    db.refresh(session)
    return (session, user)


def _local_date_for_datetime(value: datetime) -> date:
    local_value = utc_to_local(value)
    if local_value is None:
        raise ValueError("datetime value is required")
    return local_value.date()


def _minutes_to_local_datetime(work_date: date, minutes_value: int | None) -> datetime | None:
    clock_value = format_minutes_as_clock(minutes_value)
    if clock_value is None:
        return None
    return build_local_datetime(work_date, clock_value)


def _hours_value_from_minutes(minutes_value: int | None) -> float | None:
    return format_hours_from_minutes(minutes_value)


def _policy_band_rows() -> list[TimeClockPolicyBandOut]:
    rows: list[TimeClockPolicyBandOut] = []
    for band in BREAK_POLICY_BANDS:
        rows.append(
            TimeClockPolicyBandOut(
                label=band.label,
                min_span=format_minutes_as_clock(band.min_minutes) or "00:00",
                max_span=format_minutes_as_clock(band.max_minutes) if band.max_minutes is not None else None,
                deduction_minutes=band.deduction_minutes,
                requires_review=band.requires_review,
            )
        )
    return rows


def build_time_clock_policy() -> TimeClockPolicyOut:
    return TimeClockPolicyOut(
        timezone=WORKPLACE_TIMEZONE_NAME,
        grace_minutes=GRACE_MINUTES,
        auto_approve_adjustment_minutes=AUTO_APPROVE_ADJUSTMENT_MINUTES,
        self_service_adjustment_limit_minutes=MAX_SELF_SERVICE_ADJUSTMENT_MINUTES,
        long_shift_review_after_minutes=LONG_SHIFT_REVIEW_THRESHOLD_MINUTES,
        allowed_early_start_roles=sorted(ALLOWED_EARLY_START_ROLES),
        bands=_policy_band_rows(),
    )


def _find_kiosk_user_by_pin(db: Session, pin: str) -> User | None:
    normalized = ensure_clock_pin_strength(pin)
    lookup = pin_lookup_key(normalized)
    user = db.scalar(select(User).where(User.clock_pin_lookup == lookup))
    if user is None:
        return None
    if not user.clock_pin_enabled or not user.clock_pin_hash:
        return None
    if not verify_password(normalized, user.clock_pin_hash):
        return None
    return user


def _time_clock_staff_out(user: User | None, employee: EmployeeRecord) -> TimeClockStaffOut:
    pin_state = _clock_pin_state(user) if user is not None else "not_set"
    return TimeClockStaffOut(
        user_id=user.id if user is not None else None,
        email=user.email if user is not None else None,
        account_role=canonicalize_user_role(user.role) if user is not None else None,
        linked_employee_id=employee.employee_id,
        employee_name=employee.name,
        employee_role=employee.role,
        pin_enabled=pin_state != "not_set",
        pin_temporary=pin_state == "temporary",
        pin_status=pin_state,
        pin_updated_at=user.clock_pin_updated_at if user is not None else None,
        is_active=user.is_active if user is not None else False,
        has_linked_account=user is not None,
    )


def _unlink_users_for_removed_employee_ids(
    db: Session,
    employee_ids: set[str],
    *,
    preserve_admin_and_manager_access: bool = True,
) -> None:
    if not employee_ids:
        return
    linked_users = db.scalars(select(User).where(User.linked_employee_id.in_(sorted(employee_ids)))).all()
    for user in linked_users:
        role = canonicalize_user_role(user.role)
        user.linked_employee_id = None
        user.clock_pin_hash = None
        user.clock_pin_lookup = None
        user.clock_pin_enabled = False
        user.clock_pin_temporary = False
        user.clock_pin_updated_at = utcnow()
        if not preserve_admin_and_manager_access or role == "view_only":
            user.is_active = False
        db.execute(delete(SessionRecord).where(SessionRecord.user_id == user.id))
        db.add(user)


def _scheduled_shift_payload(run: ScheduleRun, matches: list[AssignmentOut]) -> dict[str, int | None] | None:
    if not matches:
        return None
    start_minutes = min(parse_time_string(assignment.start) for assignment in matches)
    end_minutes = max(parse_time_string(assignment.end) for assignment in matches)
    if end_minutes <= start_minutes:
        return None
    return {
        "schedule_run_id": run.id,
        "scheduled_start_minutes": start_minutes,
        "scheduled_end_minutes": end_minutes,
        "scheduled_paid_minutes": scheduled_paid_minutes(
            format_minutes_as_clock(start_minutes) or "00:00",
            format_minutes_as_clock(end_minutes) or "00:00",
        ),
    }


def _load_scheduled_shift_for_employee(db: Session, employee_id: str, work_date: date) -> dict[str, int | None] | None:
    runs = db.scalars(
        select(ScheduleRun)
        .where(ScheduleRun.period_start <= work_date)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
    ).all()
    target_date = work_date.isoformat()
    for run in runs:
        if work_date > _schedule_run_end_date(run.period_start, run.weeks):
            continue
        matches = [
            assignment
            for assignment in extract_assignments_from_result_json(run.result_json)
            if assignment.employee_id == employee_id and assignment.date == target_date
        ]
        payload = _scheduled_shift_payload(run, matches)
        if payload is not None:
            return payload
    return None


def _load_scheduled_shift_for_role(
    db: Session,
    *,
    role: str,
    work_date: date,
    location: str | None = None,
) -> dict[str, int | None] | None:
    runs = db.scalars(
        select(ScheduleRun)
        .where(ScheduleRun.period_start <= work_date)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
    ).all()
    target_date = work_date.isoformat()
    for run in runs:
        if work_date > _schedule_run_end_date(run.period_start, run.weeks):
            continue
        matches = [
            assignment
            for assignment in extract_assignments_from_result_json(run.result_json)
            if assignment.role == role
            and assignment.date == target_date
            and (location is None or assignment.location == location)
        ]
        payload = _scheduled_shift_payload(run, matches)
        if payload is not None:
            return payload
    return None


def _load_effective_scheduled_shift_for_employee(
    db: Session,
    *,
    employee_id: str,
    employee_role: str | None,
    work_date: date,
) -> dict[str, int | None] | None:
    scheduled_shift = _load_scheduled_shift_for_employee(db, employee_id, work_date)
    if scheduled_shift is not None:
        return scheduled_shift
    if employee_role == CAPTAIN_ROLE:
        return _load_scheduled_shift_for_role(db, role=CAPTAIN_ROLE, work_date=work_date, location="Boat")
    return None


def _maybe_auto_close_captain_record(
    record: AttendanceRecord,
    *,
    current_local: datetime,
    include_current_day: bool,
) -> bool:
    if record.status != "open" or record.role_snapshot != CAPTAIN_ROLE:
        return False
    if not captain_shift_is_full_day(record.scheduled_start_minutes, record.scheduled_end_minutes):
        return False
    if current_local.date() < record.work_date:
        return False
    if current_local.date() == record.work_date:
        captain_end_local = build_local_datetime(record.work_date, format_minutes_as_clock(CAPTAIN_STANDARD_END_MINUTES) or "17:00")
        if not include_current_day or current_local < captain_end_local:
            return False
    auto_clock_out_local = build_local_datetime(record.work_date, format_minutes_as_clock(CAPTAIN_STANDARD_END_MINUTES) or "17:00")
    effective_in_local = utc_to_local(record.effective_clock_in_at)
    if effective_in_local is not None and auto_clock_out_local <= effective_in_local:
        return False
    auto_clock_out_at = local_datetime_to_utc(auto_clock_out_local)
    record.actual_clock_out_at = auto_clock_out_at
    record.effective_clock_out_at = auto_clock_out_at
    record.status = "closed"
    record.last_action_source = "auto_close"
    _recalculate_attendance_record(record)
    return True


def _auto_close_captain_records(
    db: Session,
    *,
    current_local: datetime,
    include_current_day: bool,
    user_id: int | None = None,
) -> bool:
    stmt = select(AttendanceRecord).where(
        AttendanceRecord.status == "open",
        AttendanceRecord.role_snapshot == CAPTAIN_ROLE,
    )
    if user_id is not None:
        stmt = stmt.where(AttendanceRecord.user_id == user_id)
    rows = db.scalars(stmt.order_by(AttendanceRecord.work_date.asc(), AttendanceRecord.id.asc())).all()
    changed = False
    for record in rows:
        if _maybe_auto_close_captain_record(record, current_local=current_local, include_current_day=include_current_day):
            db.add(record)
            changed = True
    if changed:
        db.commit()
    return changed


def _serialize_attendance_record(record: AttendanceRecord) -> AttendanceRecordOut:
    return AttendanceRecordOut(
        id=record.id,
        user_id=record.user_id,
        employee_id=record.employee_id,
        employee_name=record.employee_name_snapshot,
        role=record.role_snapshot,
        work_date=record.work_date,
        scheduled_start=format_minutes_as_clock(record.scheduled_start_minutes),
        scheduled_end=format_minutes_as_clock(record.scheduled_end_minutes),
        scheduled_paid_hours=_hours_value_from_minutes(record.scheduled_paid_minutes),
        actual_clock_in_local=format_local_time(record.actual_clock_in_at),
        actual_clock_out_local=format_local_time(record.actual_clock_out_at),
        effective_clock_in_local=format_local_time(record.effective_clock_in_at),
        effective_clock_out_local=format_local_time(record.effective_clock_out_at),
        break_deduction_minutes=record.break_deduction_minutes,
        payable_hours=_hours_value_from_minutes(record.payable_minutes),
        status=record.status,
        review_state=record.review_state,
        review_note=record.review_note,
        used_scheduled_default=record.used_scheduled_default,
        last_action_source=record.last_action_source,
        updated_at=record.updated_at,
    )


def _record_has_exception(record: AttendanceRecord) -> bool:
    return record.status == "open" or record.review_state == "needs_review"


def _build_timesheet(rows: list[AttendanceRecord], *, start_date: date, end_date: date) -> TimesheetOut:
    daily_rows: list[TimesheetRowOut] = []
    totals_by_employee: dict[str, dict[str, Any]] = {}
    grouped: dict[tuple[str, date], list[AttendanceRecord]] = defaultdict(list)
    for row in rows:
        grouped[(row.employee_id, row.work_date)].append(row)

    for (_, work_date), group in sorted(
        grouped.items(),
        key=lambda item: (item[0][1], item[1][0].employee_name_snapshot.lower(), item[0][0]),
    ):
        ordered = sorted(group, key=lambda entry: (entry.effective_clock_in_at, entry.id))
        first = ordered[0]
        payable_minutes_total = sum(entry.payable_minutes or 0 for entry in ordered)
        break_minutes_total = sum(entry.break_deduction_minutes or 0 for entry in ordered)
        exception_count = sum(1 for entry in ordered if _record_has_exception(entry))
        row_out = TimesheetRowOut(
            employee_id=first.employee_id,
            employee_name=first.employee_name_snapshot,
            role=first.role_snapshot,
            work_date=work_date,
            first_in_local=format_local_time(min(entry.effective_clock_in_at for entry in ordered)),
            last_out_local=format_local_time(max(entry.effective_clock_out_at for entry in ordered if entry.effective_clock_out_at is not None)),
            payable_hours=round(payable_minutes_total / 60.0, 2),
            break_deduction_minutes=break_minutes_total,
            shift_count=len(ordered),
            exception_count=exception_count,
        )
        daily_rows.append(row_out)

        total = totals_by_employee.setdefault(
            first.employee_id,
            {
                "employee_id": first.employee_id,
                "employee_name": first.employee_name_snapshot,
                "role": first.role_snapshot,
                "payable_minutes": 0,
                "worked_days": set(),
            },
        )
        total["payable_minutes"] += payable_minutes_total
        total["worked_days"].add(work_date)

    employee_totals = [
        TimesheetEmployeeTotalOut(
            employee_id=value["employee_id"],
            employee_name=value["employee_name"],
            role=value["role"],
            payable_hours=round(value["payable_minutes"] / 60.0, 2),
            worked_days=len(value["worked_days"]),
        )
        for value in sorted(totals_by_employee.values(), key=lambda row: row["employee_name"].lower())
    ]
    grand_total_minutes = sum(int(value["payable_minutes"]) for value in totals_by_employee.values())
    return TimesheetOut(
        start_date=start_date,
        end_date=end_date,
        rows=daily_rows,
        employee_totals=employee_totals,
        grand_total_hours=round(grand_total_minutes / 60.0, 2),
    )


def _record_review_state(notes: list[str], current: str = "clear") -> tuple[str, str | None]:
    cleaned = [note.strip() for note in notes if note and note.strip()]
    if not cleaned:
        return (current if current == "approved" else "clear", None)
    if current == "approved":
        return ("approved", "; ".join(cleaned))
    return ("needs_review", "; ".join(cleaned))


def _log_attendance_adjustment(
    db: Session,
    *,
    record: AttendanceRecord,
    requested_by_user_id: int | None,
    action: str,
    reason: str | None,
    previous_in: datetime | None,
    previous_out: datetime | None,
    new_in: datetime | None,
    new_out: datetime | None,
) -> None:
    db.add(
        AttendanceAdjustment(
            attendance_record_id=record.id,
            requested_by_user_id=requested_by_user_id,
            action=action,
            reason=(reason or "").strip() or None,
            previous_effective_clock_in_at=previous_in,
            previous_effective_clock_out_at=previous_out,
            new_effective_clock_in_at=new_in,
            new_effective_clock_out_at=new_out,
        )
    )


def _recalculate_attendance_record(record: AttendanceRecord) -> None:
    if record.effective_clock_out_at is None:
        return
    effective_in_local = utc_to_local(record.effective_clock_in_at)
    effective_out_local = utc_to_local(record.effective_clock_out_at)
    if effective_in_local is None or effective_out_local is None:
        return
    schedule_start_local = _minutes_to_local_datetime(record.work_date, record.scheduled_start_minutes)
    schedule_end_local = _minutes_to_local_datetime(record.work_date, record.scheduled_end_minutes)
    allow_scheduled_default = (
        record.actual_clock_in_at == record.effective_clock_in_at
        and record.actual_clock_out_at == record.effective_clock_out_at
    )
    payable_minutes_value, break_deduction_minutes_value, used_scheduled_default = calculate_attendance_minutes(
        effective_clock_in_local=effective_in_local,
        effective_clock_out_local=effective_out_local,
        schedule_start_local=schedule_start_local,
        schedule_end_local=schedule_end_local,
        scheduled_paid_minutes_value=record.scheduled_paid_minutes,
        allow_scheduled_default=allow_scheduled_default,
    )
    record.payable_minutes = payable_minutes_value
    record.break_deduction_minutes = break_deduction_minutes_value
    record.used_scheduled_default = used_scheduled_default


def _iter_dates_inclusive(start_date: date, end_date: date) -> list[date]:
    days = (end_date - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def _schedule_run_end_date(period_start: date, weeks: int) -> date:
    span_days = max(1, int(weeks)) * 7
    return period_start + timedelta(days=span_days - 1)


def _load_schedule_ranges(db: Session, latest_end: date | None = None) -> list[tuple[date, date]]:
    stmt = select(ScheduleRun)
    if latest_end is not None:
        stmt = stmt.where(ScheduleRun.period_start <= latest_end)
    runs = db.scalars(stmt.order_by(ScheduleRun.period_start.asc(), ScheduleRun.id.asc())).all()
    return [(run.period_start, _schedule_run_end_date(run.period_start, run.weeks)) for run in runs]


def _first_locked_date_in_range(start_date: date, end_date: date, schedule_ranges: list[tuple[date, date]]) -> date | None:
    first_lock: date | None = None
    for run_start, run_end in schedule_ranges:
        if run_end < start_date or run_start > end_date:
            continue
        overlap_start = max(start_date, run_start)
        if first_lock is None or overlap_start < first_lock:
            first_lock = overlap_start
    return first_lock


def _find_first_scheduled_date_in_range(db: Session, start_date: date, end_date: date) -> date | None:
    schedule_ranges = _load_schedule_ranges(db, latest_end=end_date)
    return _first_locked_date_in_range(start_date, end_date, schedule_ranges)


def _ranges_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end


def _request_is_locked_by_schedule(db: Session, request: DayOffRequest) -> bool:
    return _find_first_scheduled_date_in_range(db, request.start_date, request.end_date) is not None


def _serialize_day_off_request(
    request_row: DayOffRequest,
    *,
    requester_email: str | None = None,
    employee_name: str | None = None,
    locked_by_schedule: bool = False,
) -> DayOffRequestOut:
    return DayOffRequestOut(
        id=request_row.id,
        requester_user_id=request_row.requester_user_id,
        requester_email=requester_email,
        employee_id=request_row.employee_id,
        employee_name=employee_name,
        start_date=request_row.start_date,
        end_date=request_row.end_date,
        reason=request_row.request_reason or "",
        status=request_row.status,
        decision_reason=request_row.decision_reason,
        cancelled_reason=request_row.cancelled_reason,
        cancelled_by_role=request_row.cancelled_by_role,
        created_at=request_row.created_at,
        decided_at=request_row.decided_at,
        cancelled_at=request_row.cancelled_at,
        locked_by_schedule=locked_by_schedule,
    )


def _approved_day_off_entries_for_range(
    db: Session,
    start_date: date,
    end_date: date,
) -> list[ApprovedDayOffEntryOut]:
    approved_rows = db.scalars(
        select(DayOffRequest)
        .where(
            DayOffRequest.status == "approved",
            DayOffRequest.start_date <= end_date,
            DayOffRequest.end_date >= start_date,
        )
        .order_by(DayOffRequest.start_date.asc(), DayOffRequest.id.asc())
    ).all()
    employee_name_by_id = {row.employee_id: row.name for row in db.scalars(select(EmployeeRecord)).all()}
    schedule_ranges = _load_schedule_ranges(db, latest_end=end_date)
    entries: list[ApprovedDayOffEntryOut] = []
    for request_row in approved_rows:
        overlap_start = max(start_date, request_row.start_date)
        overlap_end = min(end_date, request_row.end_date)
        for day in _iter_dates_inclusive(overlap_start, overlap_end):
            if _first_locked_date_in_range(day, day, schedule_ranges) is not None:
                continue
            entries.append(
                ApprovedDayOffEntryOut(
                    request_id=request_row.id,
                    employee_id=request_row.employee_id,
                    employee_name=employee_name_by_id.get(request_row.employee_id),
                    date=day,
                    reason=request_row.request_reason or "",
                )
            )
    entries.sort(key=lambda item: (item.date, item.employee_name or item.employee_id, item.request_id))
    return entries


def _day_off_requests_for_range(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    statuses: set[DayOffRequestStatus] | None = None,
) -> list[DayOffRequestOut]:
    stmt = select(DayOffRequest).where(
        DayOffRequest.start_date <= end_date,
        DayOffRequest.end_date >= start_date,
    )
    if statuses:
        stmt = stmt.where(DayOffRequest.status.in_(sorted(statuses)))
    rows = db.scalars(
        stmt.order_by(DayOffRequest.start_date.asc(), DayOffRequest.created_at.asc(), DayOffRequest.id.asc())
    ).all()
    requester_email_by_id = {row.id: row.email for row in db.scalars(select(User)).all()}
    employee_name_by_id = {row.employee_id: row.name for row in db.scalars(select(EmployeeRecord)).all()}
    schedule_ranges = _load_schedule_ranges(db)
    return [
        _serialize_day_off_request(
            row,
            requester_email=requester_email_by_id.get(row.requester_user_id),
            employee_name=employee_name_by_id.get(row.employee_id),
            locked_by_schedule=_first_locked_date_in_range(row.start_date, row.end_date, schedule_ranges) is not None,
        )
        for row in rows
    ]


def _merge_unavailability_with_approved_day_off(
    payload: GenerateRequest,
    approved_entries: list[ApprovedDayOffEntryOut],
) -> list[Unavailability]:
    merged: dict[tuple[str, date], Unavailability] = {}
    for existing in payload.unavailability:
        merged[(existing.employee_id, existing.date)] = existing
    for approved in approved_entries:
        key = (approved.employee_id, approved.date)
        if key in merged:
            continue
        merged[key] = Unavailability(employee_id=approved.employee_id, date=approved.date, reason=approved.reason or "Approved day-off request")
    return list(merged.values())


def ensure_active_admin_remains(db: Session, target_user: User, patch: UserPatchPayload) -> None:
    next_role_raw = patch.role if patch.role is not None else target_user.role
    next_role = canonicalize_user_role(str(next_role_raw))
    next_is_active = patch.is_active if patch.is_active is not None else target_user.is_active
    if canonicalize_user_role(target_user.role) != "admin" or target_user.is_active is False:
        return
    if next_role == "admin" and next_is_active:
        return
    active_admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
    if active_admin_count <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one active admin must remain")


def raise_user_write_error(exc: IntegrityError) -> None:
    message = str(getattr(exc, "orig", exc)).lower()
    if "ck_users_role" in message:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User role update failed because the database schema is out of date. Run migrations, then try again.",
        ) from exc
    if "linked_employee_id" in message and "unique" in message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That roster employee is already linked to another account") from exc
    if "users.email" in message and "unique" in message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists") from exc
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to save user changes") from exc


class Period(BaseModel):
    start_date: date
    weeks: int = Field(default=2)


class SeasonRules(BaseModel):
    victoria_day: date
    june_30: date
    labour_day: date
    oct_31: date


class HoursRange(BaseModel):
    start: str
    end: str


class Hours(BaseModel):
    greystones: HoursRange
    beach_shop: HoursRange


class Coverage(BaseModel):
    greystones_weekday_staff: int
    greystones_weekend_staff: int
    beach_shop_staff: int


class LeadershipRules(BaseModel):
    min_team_leaders_every_open_day: int
    weekend_team_leaders_if_manager_off: int
    manager_two_consecutive_days_off_per_week: bool
    manager_min_weekends_per_month: int


Role = Literal["Store Clerk", "Team Leader", "Store Manager", "Boat Captain"]
DayKey = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Employee(BaseModel):
    id: str
    name: str
    role: Role
    min_hours_per_week: int
    max_hours_per_week: int
    priority_tier: Literal["A", "B", "C"]
    student: bool = False
    availability: dict[str, list[str]]


class Unavailability(BaseModel):
    employee_id: str
    date: date
    reason: str = ""


class ExtraCoverageDay(BaseModel):
    date: date
    extra_people: int = 1


class AdHocBooking(BaseModel):
    employee_id: str
    date: date
    start: str
    end: str
    location: Literal["Greystones", "Beach Shop", "Boat"] = "Greystones"
    note: str = ""


class History(BaseModel):
    manager_weekends_worked_this_month: int = 0


class GenerateRequest(BaseModel):
    period: Period
    season_rules: SeasonRules
    hours: Hours
    coverage: Coverage
    leadership_rules: LeadershipRules
    employees: list[Employee]
    unavailability: list[Unavailability] = Field(default_factory=list)
    extra_coverage_days: list[ExtraCoverageDay] = Field(default_factory=list)  # deprecated: replaced by ad_hoc_bookings
    ad_hoc_bookings: list[AdHocBooking] = Field(default_factory=list)
    history: History = Field(default_factory=History)
    open_weekdays: list[DayKey] = Field(default_factory=lambda: DAY_KEYS.copy())
    week_start_day: DayKey = "sun"
    week_end_day: DayKey = "sat"
    reroll_token: int = Field(default=0, ge=0)
    schedule_beach_shop: bool = False
    shoulder_season: bool = False

    @model_validator(mode="after")
    def validate_week_boundaries(self) -> GenerateRequest:
        start_idx = DAY_KEYS.index(self.week_start_day)
        end_idx = DAY_KEYS.index(self.week_end_day)
        if (end_idx - start_idx) % 7 != 6:
            raise ValueError("week_start_day and week_end_day must define a full 7-day week boundary")
        if self.schedule_beach_shop and self.shoulder_season:
            raise ValueError("Shoulder season and Beach Shop scheduling cannot both be enabled")
        return self


class AssignmentOut(BaseModel):
    date: str
    location: Literal["Greystones", "Beach Shop", "Boat"]
    start: str
    end: str
    employee_id: str
    employee_name: str
    role: Role
    source: Literal["generated", "ad_hoc"] = "generated"


class ViewOnlyScheduleOut(ScheduleRunMetaOut):
    assignments: list[AssignmentOut]


class TotalsOut(BaseModel):
    week1_hours: float = 0
    week2_hours: float = 0
    week1_days: float = 0
    week2_days: float = 0
    weekend_days: int = 0
    locations: dict[str, int] = Field(default_factory=lambda: {"Greystones": 0, "Beach Shop": 0, "Boat": 0})


class ViolationOut(BaseModel):
    date: str
    type: Literal[
        "coverage_gap",
        "leader_gap",
        "manager_consecutive_days_off",
        "role_missing",
        "beach_shop_gap",
        "manager_days_rule",
        "hours_min_violation",
        "hours_max_violation",
        "ad_hoc_conflict",
    ]
    detail: str


class GenerateResponse(BaseModel):
    assignments: list[AssignmentOut]
    totals_by_employee: dict[str, TotalsOut]
    violations: list[ViolationOut]


DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2}
BOAT_SHIFT_START = "09:00"
BOAT_SHIFT_END = "17:00"


def _time_to_minutes(value: str) -> int:
    return parse_time_string(value)


def _hours_between(start: str, end: str) -> float:
    span_total = _time_to_minutes(end) - _time_to_minutes(start)
    return round(payable_minutes_for_span(span_total) / 60.0, 2)


def _format_hours(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _daterange(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def _next_or_same_day(d: date, target_day: DayKey) -> date:
    days_until = (DAY_KEYS.index(target_day) - d.weekday()) % 7
    return d + timedelta(days=days_until)


def _next_sunday_after(d: date) -> date:
    days_until_next = (DAY_KEYS.index("sun") - d.weekday()) % 7
    if days_until_next == 0:
        days_until_next = 7
    return d + timedelta(days=days_until_next)


def _first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def _victoria_day(year: int) -> date:
    d = date(year, 5, 24)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    return d


def _season_rules_for_year(year: int) -> SeasonRules:
    return SeasonRules(
        victoria_day=_victoria_day(year),
        june_30=date(year, 6, 30),
        labour_day=_first_monday(year, 9),
        oct_31=date(year, 10, 31),
    )


def _normalized_season_rules(start_date: date, provided: SeasonRules) -> SeasonRules:
    years = {provided.victoria_day.year, provided.june_30.year, provided.labour_day.year, provided.oct_31.year}
    if years == {start_date.year}:
        return provided
    return _season_rules_for_year(start_date.year)


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _is_greystones_open(d: date, s: SeasonRules) -> bool:
    july_1 = date(d.year, 7, 1)
    if s.victoria_day <= d <= s.june_30:
        return d.weekday() >= 4
    if july_1 <= d <= s.labour_day:
        return True
    if (s.labour_day + timedelta(days=1)) <= d <= s.oct_31:
        return d.weekday() >= 4
    return False


def _is_beach_shop_open(d: date, s: SeasonRules) -> bool:
    july_1 = date(d.year, 7, 1)
    # Beach Shop runs on weekends year-round and can run on weekdays during peak summer.
    return d.weekday() >= 5 or (july_1 <= d <= s.labour_day)


def _week_index(day: date, start: date) -> int:
    return ((day - start).days // 7) + 1


def _week_start_for(day: date, schedule_start: date) -> date:
    return schedule_start + timedelta(days=((day - schedule_start).days // 7) * 7)


def _reroll_rank(employee_id: str, reroll_token: int) -> int:
    digest = hashlib.blake2b(f"{reroll_token}:{employee_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _choose_pair_for_manager_off(days: list[date], season: SeasonRules, extras: dict[date, int]) -> tuple[date, date]:
    pairs = []
    preferred_default_pair = (1, 2)  # Tuesday-Wednesday
    for i in range(len(days) - 1):
        d1, d2 = days[i], days[i + 1]
        score = int(_is_greystones_open(d1, season)) + int(_is_greystones_open(d2, season))
        score += extras.get(d1, 0) + extras.get(d2, 0)
        weekend_penalty = int(_is_weekend(d1)) + int(_is_weekend(d2))
        # Prefer weekday pairs for manager days off so weekends remain manager-covered by default.
        # When all other factors tie, default to Tuesday-Wednesday.
        default_pair_penalty = 0 if (d1.weekday(), d2.weekday()) == preferred_default_pair else 1
        pairs.append((weekend_penalty, score, default_pair_penalty, d1, d2))
    pairs.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    return (pairs[0][3], pairs[0][4]) if pairs else (days[0], days[0])


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _build_weekly_history_from_run(
    run: ScheduleRun,
) -> tuple[dict[tuple[date, str], float], dict[tuple[date, str], int], dict[tuple[date, str], set[date]]]:
    payload_json = run.payload_json or {}
    result_json = run.result_json or {}
    period = payload_json.get("period", {}) if isinstance(payload_json, dict) else {}
    raw_start = period.get("start_date") if isinstance(period, dict) else None
    run_start = _parse_iso_date(raw_start)
    if run_start is None:
        return ({}, {}, {})
    week_start_day = payload_json.get("week_start_day") if isinstance(payload_json, dict) else "sun"
    if week_start_day not in DAY_KEYS:
        week_start_day = "sun"
    run_start = _next_or_same_day(run_start, week_start_day)

    assignments = result_json.get("assignments") if isinstance(result_json, dict) else []
    if not isinstance(assignments, list):
        return ({}, {}, {})

    day_max_hours: dict[tuple[str, date], float] = defaultdict(float)
    leader_days: dict[tuple[date, str], set[date]] = defaultdict(set)
    worked_days: dict[tuple[date, str], set[date]] = defaultdict(set)
    for raw in assignments:
        if not isinstance(raw, dict):
            continue
        employee_id = raw.get("employee_id")
        shift_date = _parse_iso_date(raw.get("date"))
        if not employee_id or shift_date is None:
            continue
        raw_start_time = raw.get("start")
        raw_end_time = raw.get("end")
        if not isinstance(raw_start_time, str) or not isinstance(raw_end_time, str):
            continue
        shift_hours = _hours_between(raw_start_time, raw_end_time)
        day_key = (str(employee_id), shift_date)
        day_max_hours[day_key] = max(day_max_hours[day_key], shift_hours)
        week_start = _week_start_for(shift_date, run_start)
        worked_days[(week_start, str(employee_id))].add(shift_date)
        if raw.get("role") == "Team Leader" and raw.get("location") == "Greystones":
            leader_days[(week_start, str(employee_id))].add(shift_date)

    weekly_hours: dict[tuple[date, str], float] = defaultdict(float)
    for (employee_id, shift_day), hours in day_max_hours.items():
        week_start = _week_start_for(shift_day, run_start)
        weekly_hours[(week_start, employee_id)] += hours

    leader_day_counts = {(ws, emp_id): len(days) for (ws, emp_id), days in leader_days.items()}
    weekly_work_days = {(ws, emp_id): set(days) for (ws, emp_id), days in worked_days.items()}
    return (dict(weekly_hours), leader_day_counts, weekly_work_days)


def _load_generation_history_maps(
    db: Session,
    payload: GenerateRequest,
) -> tuple[dict[tuple[date, str], float], dict[tuple[date, str], int], dict[tuple[date, str], set[date]]]:
    schedule_start = _next_or_same_day(payload.period.start_date, payload.week_start_day)
    runs = db.scalars(
        select(ScheduleRun)
        .where(ScheduleRun.period_start < schedule_start)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
    ).all()

    # Use only the most recent finalized snapshot per week start.
    latest_week_stamp: dict[date, tuple[datetime, int]] = {}
    weekly_hours: dict[tuple[date, str], float] = {}
    leader_days: dict[tuple[date, str], int] = {}
    weekly_work_days: dict[tuple[date, str], set[date]] = {}
    for run in runs:
        run_weekly_hours, run_leader_days, run_weekly_work_days = _build_weekly_history_from_run(run)
        for key, hours in run_weekly_hours.items():
            week_start, _employee_id = key
            stamp = (run.created_at, run.id)
            if week_start in latest_week_stamp and stamp <= latest_week_stamp[week_start]:
                continue
            # Drop stale entries for the same week when a newer finalized run exists.
            for prior_key in [k for k in weekly_hours if k[0] == week_start]:
                weekly_hours.pop(prior_key, None)
            for prior_key in [k for k in leader_days if k[0] == week_start]:
                leader_days.pop(prior_key, None)
            for prior_key in [k for k in weekly_work_days if k[0] == week_start]:
                weekly_work_days.pop(prior_key, None)
            latest_week_stamp[week_start] = stamp
            for run_key, value in run_weekly_hours.items():
                if run_key[0] == week_start:
                    weekly_hours[run_key] = round(float(value), 2)
            for run_key, value in run_leader_days.items():
                if run_key[0] == week_start:
                    leader_days[run_key] = int(value)
            for run_key, value in run_weekly_work_days.items():
                if run_key[0] == week_start:
                    weekly_work_days[run_key] = set(value)
    return (weekly_hours, leader_days, weekly_work_days)


def _generate(
    payload: GenerateRequest,
    history_weekly_hours: dict[tuple[date, str], float] | None = None,
    history_weekly_leader_days: dict[tuple[date, str], int] | None = None,
    history_weekly_work_days: dict[tuple[date, str], set[date]] | None = None,
) -> GenerateResponse:
    start_date = _next_or_same_day(payload.period.start_date, payload.week_start_day)
    season_rules = _normalized_season_rules(start_date, payload.season_rules)
    emp_map = {e.id: e for e in sorted(payload.employees, key=lambda x: x.id)}
    unavail = {(u.employee_id, u.date) for u in payload.unavailability}
    all_days = _daterange(start_date, payload.period.weeks * 7)
    week_starts = [start_date + timedelta(days=7 * i) for i in range(payload.period.weeks)]
    open_weekdays = set(payload.open_weekdays or DAY_KEYS)
    history_weekly_hours = history_weekly_hours or {}
    history_weekly_leader_days = history_weekly_leader_days or {}
    history_weekly_work_days = history_weekly_work_days or {}
    prior_week_start = start_date - timedelta(days=7)
    prior_week_worked_days: dict[str, set[date]] = {
        employee_id: set(history_weekly_work_days.get((prior_week_start, employee_id), set()))
        for employee_id in emp_map
    }

    def is_store_open(day: date) -> bool:
        return DAY_KEYS[day.weekday()] in open_weekdays

    open_days = [d for d in all_days if is_store_open(d)]
    open_day_index = {d: i for i, d in enumerate(open_days)}
    lead_ids = sorted([e.id for e in emp_map.values() if e.role == "Team Leader"])
    lead_pair = tuple(lead_ids[:2]) if len(lead_ids) == 2 else ()
    rotation_target_by_week: dict[date, str | None] = {}
    clerk_ids = [e.id for e in emp_map.values() if e.role == "Store Clerk"]
    clerk_lookback_hours: dict[str, float] = {}
    if not payload.shoulder_season:
        for clerk_id in clerk_ids:
            lookback_total = 0.0
            for weeks_ago in range(1, 5):
                prior_week = start_date - timedelta(days=7 * weeks_ago)
                lookback_total += history_weekly_hours.get((prior_week, clerk_id), 0.0)
            clerk_lookback_hours[clerk_id] = round(lookback_total, 2)
    ad_hoc_by_day: dict[date, list[AdHocBooking]] = defaultdict(list)
    for booking in payload.ad_hoc_bookings:
        if booking.date in all_days:
            ad_hoc_by_day[booking.date].append(booking)
    for day_bookings in ad_hoc_by_day.values():
        day_bookings.sort(key=lambda b: (b.start, b.employee_id, b.location))

    assignments: list[dict] = []
    violations: list[ViolationOut] = []
    daily_assigned: dict[date, set[str]] = defaultdict(set)
    daily_hours_counted: dict[tuple[str, date], float] = defaultdict(float)
    weekly_hours: dict[tuple[str, int], float] = defaultdict(float)
    weekly_days: dict[tuple[str, int], int] = defaultdict(int)
    weekly_store_leader_days: dict[tuple[str, int], set[date]] = defaultdict(set)
    requested_days_off_by_week: dict[tuple[str, int], int] = defaultdict(int)
    for employee_id, day in unavail:
        if day in all_days and is_store_open(day):
            requested_days_off_by_week[(employee_id, _week_index(day, start_date))] += 1

    manager_ids = [e.id for e in emp_map.values() if e.role == "Store Manager"]
    manager_vacations_by_week: dict[tuple[str, date], int] = defaultdict(int)
    for manager_id in manager_ids:
        for day in all_days:
            if (manager_id, day) in unavail:
                week_start = _week_start_for(day, start_date)
                manager_vacations_by_week[(manager_id, week_start)] += 1

    forced_manager_off: set[date] = set()
    if payload.leadership_rules.manager_two_consecutive_days_off_per_week and manager_ids and not payload.shoulder_season:
        for ws in week_starts:
            week_days = [ws + timedelta(days=i) for i in range(7) if ws + timedelta(days=i) in all_days]
            if week_days:
                for manager_id in manager_ids:
                    if manager_vacations_by_week[(manager_id, ws)] > 0:
                        continue
                    week_open_days = [d for d in week_days if is_store_open(d)]
                    if len(week_open_days) < 2:
                        continue
                    a, b = _choose_pair_for_manager_off(week_open_days, season_rules, {})
                    forced_manager_off.update({a, b})

    def _rotation_target_from_counts(day_counts: dict[str, int]) -> str | None:
        if len(lead_pair) != 2:
            return None
        lead_a, lead_b = lead_pair
        a_days = day_counts.get(lead_a, 0)
        b_days = day_counts.get(lead_b, 0)
        if a_days > b_days:
            return lead_b
        if b_days > a_days:
            return lead_a
        return None

    def _lead_rotation_target_for_week(week_start: date) -> str | None:
        if payload.shoulder_season or len(lead_pair) != 2:
            return None
        if week_start in rotation_target_by_week:
            return rotation_target_by_week[week_start]
        if week_start == start_date:
            prior_week = week_start - timedelta(days=7)
            prior_counts = {
                lead_pair[0]: int(history_weekly_leader_days.get((prior_week, lead_pair[0]), 0)),
                lead_pair[1]: int(history_weekly_leader_days.get((prior_week, lead_pair[1]), 0)),
            }
            rotation_target_by_week[week_start] = _rotation_target_from_counts(prior_counts)
            return rotation_target_by_week[week_start]
        prior_week = week_start - timedelta(days=7)
        prior_idx = _week_index(prior_week, start_date)
        prior_counts = {
            lead_pair[0]: len(weekly_store_leader_days[(lead_pair[0], prior_idx)]),
            lead_pair[1]: len(weekly_store_leader_days[(lead_pair[1], prior_idx)]),
        }
        rotation_target_by_week[week_start] = _rotation_target_from_counts(prior_counts)
        return rotation_target_by_week[week_start]

    def prior_open_day_off_streak(employee_id: str, day: date) -> int:
        day_idx = open_day_index.get(day)
        if day_idx is None:
            return 0
        streak = 0
        for idx in range(day_idx - 1, -1, -1):
            prev_day = open_days[idx]
            if employee_id in daily_assigned[prev_day]:
                break
            streak += 1
        return streak

    def prior_consecutive_days_worked(employee_id: str, day: date) -> int:
        streak = 0
        cursor = day - timedelta(days=1)
        while True:
            if cursor >= start_date:
                if employee_id in daily_assigned[cursor]:
                    streak += 1
                    cursor -= timedelta(days=1)
                    continue
                break
            if cursor >= prior_week_start and cursor in prior_week_worked_days.get(employee_id, set()):
                streak += 1
                cursor -= timedelta(days=1)
                continue
            break
        return streak

    def eligible(day: date, role: Role, start: str, end: str, ignore_max: bool = False, allow_double_booking: bool = False) -> list[Employee]:
        smin = _time_to_minutes(start)
        emin = _time_to_minutes(end)
        out: list[Employee] = []
        for e in emp_map.values():
            if e.role != role:
                continue
            if payload.shoulder_season and e.student and day.weekday() < 5:
                continue
            if (e.id, day) in unavail:
                continue
            if e.id not in daily_assigned[day] and prior_consecutive_days_worked(e.id, day) >= 5:
                continue
            if not allow_double_booking and e.id in daily_assigned[day]:
                continue
            if role == "Store Manager" and day in forced_manager_off:
                continue
            wk = _week_index(day, start_date)
            if role == "Store Manager" and (not payload.shoulder_season) and weekly_days[(e.id, wk)] >= 5:
                continue
            if not ignore_max and weekly_hours[(e.id, wk)] + _hours_between(start, end) > e.max_hours_per_week:
                continue
            windows = e.availability.get(DAY_KEYS[day.weekday()], [])
            fits = any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)
            if not fits:
                continue
            out.append(e)

        def work_pattern_penalty(employee_id: str) -> tuple[int, int]:
            yesterday = day - timedelta(days=1)
            two_days_ago = day - timedelta(days=2)
            worked_yesterday = yesterday in all_days and employee_id in daily_assigned[yesterday]
            worked_two_days_ago = two_days_ago in all_days and employee_id in daily_assigned[two_days_ago]
            starts_new_on_block = 0 if worked_yesterday else 1
            breaks_single_day_off = 1 if (not worked_yesterday and worked_two_days_ago) else 0
            return (starts_new_on_block, breaks_single_day_off)

        def off_streak_priority(employee_id: str) -> tuple[int, int]:
            streak = prior_open_day_off_streak(employee_id, day)
            if streak >= 3:
                return (0, -streak)
            if streak >= 2:
                return (1, 0)
            return (2, 0)

        def role_fairness_key(employee: Employee, week_idx: int) -> tuple[int, int, float]:
            if role == "Team Leader" and len(lead_pair) == 2 and not payload.shoulder_season:
                week_start = _week_start_for(day, start_date)
                preferred = _lead_rotation_target_for_week(week_start)
                if preferred in lead_pair:
                    other = lead_pair[1] if preferred == lead_pair[0] else lead_pair[0]
                    preferred_count = len(weekly_store_leader_days[(preferred, week_idx)])
                    other_count = len(weekly_store_leader_days[(other, week_idx)])
                    if employee.id == preferred:
                        new_diff = (preferred_count + 1) - other_count
                    else:
                        new_diff = preferred_count - (other_count + 1)
                    return (0, abs(new_diff - 1), 0.0 if employee.id == preferred else 1.0)
            if role == "Store Clerk":
                lookback_base = 0.0 if payload.shoulder_season else clerk_lookback_hours.get(employee.id, 0.0)
                return (
                    1,
                    PRIORITY_ORDER[employee.priority_tier],
                    round(lookback_base + weekly_hours[(employee.id, week_idx)], 2),
                )
            return (2, 0, 0.0)

        def max_hours_preference_key(employee: Employee, week_idx: int) -> tuple[int, float, int]:
            projected = weekly_hours[(employee.id, week_idx)] + _hours_between(start, end)
            overtime = max(0.0, round(projected - employee.max_hours_per_week, 2))
            if overtime == 0:
                # Normal priority ordering (A before B before C) when max-hours are respected.
                return (0, 0.0, PRIORITY_ORDER[employee.priority_tier])
            # If overtime is unavoidable, prefer lower-tier employees first to protect high-tier staff.
            overtime_priority = max(PRIORITY_ORDER.values()) - PRIORITY_ORDER[employee.priority_tier]
            return (1, overtime, overtime_priority)

        wk = _week_index(day, start_date)
        if role == "Store Clerk":
            out.sort(key=lambda e: (
                max_hours_preference_key(e, wk),
                role_fairness_key(e, wk),
                off_streak_priority(e.id),
                work_pattern_penalty(e.id),
                weekly_hours[(e.id, wk)],
                _reroll_rank(e.id, payload.reroll_token),
                e.name,
            ))
        else:
            out.sort(key=lambda e: (
                off_streak_priority(e.id),
                work_pattern_penalty(e.id),
                max_hours_preference_key(e, wk),
                role_fairness_key(e, wk),
                weekly_hours[(e.id, wk)],
                _reroll_rank(e.id, payload.reroll_token),
                e.name,
            ))
        return out

    def add_assignment(
        day: date,
        location: str,
        start: str,
        end: str,
        employee: Employee,
        role: Role,
        source: Literal["generated", "ad_hoc"] = "generated",
    ):
        assignments.append({
            "date": day,
            "location": location,
            "start": start,
            "end": end,
            "employee_id": employee.id,
            "employee_name": employee.name,
            "role": role,
            "source": source,
        })
        wk = _week_index(day, start_date)
        shift_hours = _hours_between(start, end)
        day_key = (employee.id, day)
        prior_counted = daily_hours_counted[day_key]
        new_counted = max(prior_counted, shift_hours)
        weekly_hours[(employee.id, wk)] += new_counted - prior_counted
        daily_hours_counted[day_key] = new_counted
        if employee.id not in daily_assigned[day]:
            weekly_days[(employee.id, wk)] += 1
        daily_assigned[day].add(employee.id)
        if role == "Team Leader" and location == "Greystones":
            weekly_store_leader_days[(employee.id, wk)].add(day)

    def assign_one(day: date, location: str, start: str, end: str, role: Role, needed: int, ignore_max: bool = False, allow_double_booking: bool = False):
        assigned_ids: set[str] = set()
        # Always try max-safe candidates first; exceed max only as fallback when explicitly allowed.
        for e in eligible(day, role, start, end, ignore_max=False, allow_double_booking=allow_double_booking):
            if len(assigned_ids) >= needed:
                break
            add_assignment(day, location, start, end, e, role)
            assigned_ids.add(e.id)
        if ignore_max and len(assigned_ids) < needed:
            for e in eligible(day, role, start, end, ignore_max=True, allow_double_booking=allow_double_booking):
                if len(assigned_ids) >= needed:
                    break
                if e.id in assigned_ids:
                    continue
                add_assignment(day, location, start, end, e, role)
                assigned_ids.add(e.id)

    def _is_floor_staff_assigned(employee_id: str, day: date) -> bool:
        return any(
            a["date"] == day
            and a["location"] == "Greystones"
            and a["employee_id"] == employee_id
            and a["role"] in {"Team Leader", "Store Clerk"}
            for a in assignments
        )

    def assign_beach_staff(day: date, start: str, end: str, needed: int) -> int:
        beach_assigned_ids = {
            a["employee_id"]
            for a in assignments
            if a["date"] == day and a["location"] == "Beach Shop"
        }
        floor_pulls = sum(1 for employee_id in beach_assigned_ids if _is_floor_staff_assigned(employee_id, day))
        max_floor_pulls = 1

        def assign_for_role(role: Role, role_needed: int) -> int:
            nonlocal floor_pulls
            assigned = 0
            for ignore_max in (False, True):
                for e in eligible(day, role, start, end, ignore_max=ignore_max, allow_double_booking=False):
                    if assigned >= role_needed:
                        break
                    if e.id in beach_assigned_ids:
                        continue
                    add_assignment(day, "Beach Shop", start, end, e, role)
                    beach_assigned_ids.add(e.id)
                    assigned += 1
                if assigned >= role_needed:
                    return assigned

            if floor_pulls >= max_floor_pulls:
                return assigned

            for ignore_max in (False, True):
                for e in eligible(day, role, start, end, ignore_max=ignore_max, allow_double_booking=True):
                    if assigned >= role_needed or floor_pulls >= max_floor_pulls:
                        break
                    if e.id in beach_assigned_ids:
                        continue
                    if not _is_floor_staff_assigned(e.id, day):
                        continue
                    add_assignment(day, "Beach Shop", start, end, e, role)
                    beach_assigned_ids.add(e.id)
                    floor_pulls += 1
                    assigned += 1
                if assigned >= role_needed or floor_pulls >= max_floor_pulls:
                    break
            return assigned

        remaining = max(0, needed)
        # Strong preference: fill Beach Shop slots with Store Clerks before Team Leaders.
        remaining -= assign_for_role("Store Clerk", remaining)
        if remaining > 0:
            remaining -= assign_for_role("Team Leader", remaining)
        return needed - remaining

    def _makeup_shift_for(role: Role) -> tuple[str, str, str]:
        greystones_shift = ("Greystones", payload.hours.greystones.start, payload.hours.greystones.end)
        if role == "Boat Captain":
            return ("Boat", BOAT_SHIFT_START, BOAT_SHIFT_END)
        if role in {"Store Manager", "Team Leader", "Store Clerk"}:
            return greystones_shift
        return greystones_shift

    def _can_add_makeup_shift(employee: Employee, day: date, start: str, end: str) -> bool:
        if (employee.id, day) in unavail:
            return False
        if employee.id in daily_assigned[day]:
            return False
        windows = employee.availability.get(DAY_KEYS[day.weekday()], [])
        smin = _time_to_minutes(start)
        emin = _time_to_minutes(end)
        fits = any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)
        if not fits:
            return False
        wk = _week_index(day, start_date)
        shift_hours = _hours_between(start, end)
        if weekly_hours[(employee.id, wk)] + shift_hours > employee.max_hours_per_week:
            return False
        if employee.role == "Store Manager" and day in forced_manager_off:
            return False
        if employee.role == "Store Manager" and (not payload.shoulder_season) and weekly_days[(employee.id, wk)] >= 5:
            return False
        return True

    def _preferred_makeup_days(role: Role, week_days: list[date]) -> list[date]:
        if role == "Team Leader":
            return sorted(
                week_days,
                key=lambda d: (
                    0 if d.weekday() == 5 else 1 if d.weekday() == 4 else 2,
                    d,
                ),
            )
        if role == "Store Clerk":
            return sorted(
                week_days,
                key=lambda d: (
                    0 if d.weekday() == 3 else 1 if d.weekday() == 4 else 2,
                    d,
                ),
            )
        return sorted(
            week_days,
            key=lambda d: (
                0 if d.weekday() == 3 else 1 if d.weekday() == 4 else 2,
                d,
            ),
        )

    def _location_role_compatible(role: Role, location: str) -> bool:
        if location == "Boat":
            return role == "Boat Captain"
        if location == "Beach Shop":
            return role in {"Store Clerk", "Team Leader"}
        if location == "Greystones":
            return role in {"Store Clerk", "Team Leader", "Store Manager"}
        return False

    def rebuild_assignment_tracking(
        assignment_rows: list[dict],
    ) -> tuple[
        dict[date, set[str]],
        dict[tuple[str, date], float],
        dict[tuple[str, int], float],
        dict[tuple[str, int], int],
        dict[tuple[str, int], set[date]],
    ]:
        state_daily_assigned: dict[date, set[str]] = defaultdict(set)
        state_daily_hours_counted: dict[tuple[str, date], float] = defaultdict(float)
        state_weekly_hours: dict[tuple[str, int], float] = defaultdict(float)
        state_weekly_days: dict[tuple[str, int], int] = defaultdict(int)
        state_weekly_store_leader_days: dict[tuple[str, int], set[date]] = defaultdict(set)
        for assignment in assignment_rows:
            employee_id = assignment["employee_id"]
            day = assignment["date"]
            wk = _week_index(day, start_date)
            shift_hours = _hours_between(assignment["start"], assignment["end"])
            day_key = (employee_id, day)
            prior_counted = state_daily_hours_counted[day_key]
            new_counted = max(prior_counted, shift_hours)
            state_weekly_hours[(employee_id, wk)] += new_counted - prior_counted
            state_daily_hours_counted[day_key] = new_counted
            if employee_id not in state_daily_assigned[day]:
                state_weekly_days[(employee_id, wk)] += 1
            state_daily_assigned[day].add(employee_id)
            if assignment["role"] == "Team Leader" and assignment["location"] == "Greystones":
                state_weekly_store_leader_days[(employee_id, wk)].add(day)
        return (
            state_daily_assigned,
            state_daily_hours_counted,
            state_weekly_hours,
            state_weekly_days,
            state_weekly_store_leader_days,
        )

    def prior_consecutive_days_worked_with_state(employee_id: str, day: date, state_daily_assigned: dict[date, set[str]]) -> int:
        streak = 0
        cursor = day - timedelta(days=1)
        while True:
            if cursor >= start_date:
                if employee_id in state_daily_assigned.get(cursor, set()):
                    streak += 1
                    cursor -= timedelta(days=1)
                    continue
                break
            if cursor >= prior_week_start and cursor in prior_week_worked_days.get(employee_id, set()):
                streak += 1
                cursor -= timedelta(days=1)
                continue
            break
        return streak

    def overtime_by_employee_week(state_weekly_hours: dict[tuple[str, int], float]) -> dict[tuple[str, int], float]:
        overtime: dict[tuple[str, int], float] = {}
        for ws in week_starts:
            wk = _week_index(ws, start_date)
            for employee in emp_map.values():
                over = max(0.0, round(state_weekly_hours[(employee.id, wk)] - employee.max_hours_per_week, 2))
                if over > 0:
                    overtime[(employee.id, wk)] = over
        return overtime

    def can_take_existing_assignment(
        employee: Employee,
        assignment: dict,
        state_daily_assigned: dict[date, set[str]],
        state_weekly_hours: dict[tuple[str, int], float],
        state_weekly_days: dict[tuple[str, int], int],
    ) -> bool:
        day = assignment["date"]
        start = assignment["start"]
        end = assignment["end"]
        role = assignment["role"]
        if employee.role != role:
            return False
        if (employee.id, day) in unavail:
            return False
        if employee.id in state_daily_assigned.get(day, set()):
            return False
        if prior_consecutive_days_worked_with_state(employee.id, day, state_daily_assigned) >= 5:
            return False
        wk = _week_index(day, start_date)
        if role == "Store Manager" and day in forced_manager_off:
            return False
        if role == "Store Manager" and (not payload.shoulder_season) and state_weekly_days[(employee.id, wk)] >= 5:
            return False
        projected_hours = state_weekly_hours[(employee.id, wk)] + _hours_between(start, end)
        if projected_hours > employee.max_hours_per_week:
            return False
        smin = _time_to_minutes(start)
        emin = _time_to_minutes(end)
        windows = employee.availability.get(DAY_KEYS[day.weekday()], [])
        return any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)

    def rebalance_avoidable_overtime() -> None:
        nonlocal daily_assigned, daily_hours_counted, weekly_hours, weekly_days, weekly_store_leader_days
        while True:
            (
                state_daily_assigned,
                _state_daily_hours_counted,
                state_weekly_hours,
                state_weekly_days,
                _state_weekly_store_leader_days,
            ) = rebuild_assignment_tracking(assignments)
            overtime_map = overtime_by_employee_week(state_weekly_hours)
            base_total_overtime = round(sum(overtime_map.values()), 2)
            if base_total_overtime <= 0:
                break

            best_swap: dict[str, Any] | None = None
            for idx, assignment in enumerate(assignments):
                if assignment.get("source", "generated") != "generated":
                    continue
                over_employee_id = assignment["employee_id"]
                wk = _week_index(assignment["date"], start_date)
                if overtime_map.get((over_employee_id, wk), 0.0) <= 0:
                    continue
                over_employee = emp_map.get(over_employee_id)
                if over_employee is None:
                    continue

                replacement_candidates = [
                    employee
                    for employee in emp_map.values()
                    if employee.id != over_employee_id and employee.role == assignment["role"]
                ]
                replacement_candidates.sort(
                    key=lambda employee: (
                        -PRIORITY_ORDER[employee.priority_tier],
                        state_weekly_hours[(employee.id, wk)],
                        employee.name,
                    )
                )
                for replacement in replacement_candidates:
                    if not can_take_existing_assignment(replacement, assignment, state_daily_assigned, state_weekly_hours, state_weekly_days):
                        continue
                    original_employee_id = assignment["employee_id"]
                    original_employee_name = assignment["employee_name"]
                    assignment["employee_id"] = replacement.id
                    assignment["employee_name"] = replacement.name

                    (
                        _new_daily_assigned,
                        _new_daily_hours_counted,
                        new_weekly_hours,
                        _new_weekly_days,
                        _new_weekly_store_leader_days,
                    ) = rebuild_assignment_tracking(assignments)
                    new_overtime_map = overtime_by_employee_week(new_weekly_hours)
                    new_total_overtime = round(sum(new_overtime_map.values()), 2)

                    if (not payload.shoulder_season) and requested_days_off_by_week[(original_employee_id, wk)] == 0:
                        if new_weekly_hours[(original_employee_id, wk)] < over_employee.min_hours_per_week:
                            assignment["employee_id"] = original_employee_id
                            assignment["employee_name"] = original_employee_name
                            continue

                    if new_total_overtime < base_total_overtime:
                        candidate_score = (
                            round(base_total_overtime - new_total_overtime, 2),
                            -new_overtime_map.get((original_employee_id, wk), 0.0),
                            PRIORITY_ORDER[replacement.priority_tier],
                        )
                        if best_swap is None or candidate_score > best_swap["score"]:
                            best_swap = {
                                "index": idx,
                                "replacement_id": replacement.id,
                                "replacement_name": replacement.name,
                                "score": candidate_score,
                            }

                    assignment["employee_id"] = original_employee_id
                    assignment["employee_name"] = original_employee_name

            if best_swap is None:
                break

            assignments[best_swap["index"]]["employee_id"] = best_swap["replacement_id"]
            assignments[best_swap["index"]]["employee_name"] = best_swap["replacement_name"]

        (
            daily_assigned,
            daily_hours_counted,
            weekly_hours,
            weekly_days,
            weekly_store_leader_days,
        ) = rebuild_assignment_tracking(assignments)

    def apply_ad_hoc_for_day(day: date):
        for booking in ad_hoc_by_day.get(day, []):
            employee = emp_map.get(booking.employee_id)
            if employee is None:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for unknown employee id {booking.employee_id} could not be scheduled",
                    )
                )
                continue
            if not is_store_open(day):
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} could not be scheduled because the store is closed",
                    )
                )
                continue
            if not _location_role_compatible(employee.role, booking.location):
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} is not compatible with {booking.location}",
                    )
                )
                continue
            if booking.location == "Beach Shop" and not _is_beach_shop_open(day, season_rules):
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} could not be scheduled because Beach Shop is closed",
                    )
                )
                continue
            if (employee.id, day) in unavail:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} conflicts with requested time off",
                    )
                )
                continue
            if employee.id in daily_assigned[day]:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} could not be scheduled because they already have a shift that day",
                    )
                )
                continue
            try:
                smin = _time_to_minutes(booking.start)
                emin = _time_to_minutes(booking.end)
            except Exception:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} has an invalid time format",
                    )
                )
                continue
            if emin <= smin:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} has an invalid time range",
                    )
                )
                continue
            windows = employee.availability.get(DAY_KEYS[day.weekday()], [])
            fits = any(_time_to_minutes(w.split("-")[0]) <= smin and _time_to_minutes(w.split("-")[1]) >= emin for w in windows)
            if not fits:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} is outside availability",
                    )
                )
                continue
            if prior_consecutive_days_worked(employee.id, day) >= 5:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} would exceed 5 consecutive work days",
                    )
                )
                continue
            wk = _week_index(day, start_date)
            shift_hours = _hours_between(booking.start, booking.end)
            if weekly_hours[(employee.id, wk)] + shift_hours > employee.max_hours_per_week:
                violations.append(
                    ViolationOut(
                        date=day.isoformat(),
                        type="ad_hoc_conflict",
                        detail=f"Ad hoc shift for {employee.name} would exceed weekly max hours",
                    )
                )
                continue
            add_assignment(day, booking.location, booking.start, booking.end, employee, employee.role, source="ad_hoc")

    for d in all_days:
        if is_store_open(d):
            g_start, g_end = payload.hours.greystones.start, payload.hours.greystones.end
            needed = payload.coverage.greystones_weekend_staff if _is_weekend(d) else payload.coverage.greystones_weekday_staff
            assign_one(d, "Greystones", g_start, g_end, "Store Manager", 1, ignore_max=payload.shoulder_season)
            manager_on = any(a for a in assignments if a["date"] == d and a["location"] == "Greystones" and a["role"] == "Store Manager")
            if payload.shoulder_season and not manager_on:
                violations.append(ViolationOut(date=d.isoformat(), type="manager_days_rule", detail="Shoulder season requires a Store Manager on every open day"))
            manager_off = not manager_on
            manager_off_lead_target = max(2, payload.leadership_rules.weekend_team_leaders_if_manager_off)
            lead_need = max(payload.leadership_rules.min_team_leaders_every_open_day, manager_off_lead_target if manager_off else 1)
            # Manager-off lead rule should not be blocked by weekly max-hours limits.
            assign_one(d, "Greystones", g_start, g_end, "Team Leader", lead_need, ignore_max=manager_off)
            leaders_assigned = len([
                a for a in assignments
                if a["date"] == d and a["location"] == "Greystones" and a["role"] == "Team Leader"
            ])
            if leaders_assigned < lead_need:
                detail = f"Greystones needed {lead_need} Team Leader(s)"
                if manager_off:
                    detail += " because no manager was scheduled"
                violations.append(ViolationOut(date=d.isoformat(), type="leader_gap", detail=detail))

            floor_roles = {"Team Leader", "Store Clerk"}
            floor_staff_assigned = len([
                a for a in assignments
                if a["date"] == d and a["location"] == "Greystones" and a["role"] in floor_roles
            ])
            assign_one(d, "Greystones", g_start, g_end, "Store Clerk", max(0, needed - floor_staff_assigned))
            floor_staff_assigned = len([
                a for a in assignments
                if a["date"] == d and a["location"] == "Greystones" and a["role"] in floor_roles
            ])
            if floor_staff_assigned < needed:
                violations.append(ViolationOut(date=d.isoformat(), type="coverage_gap", detail=f"Greystones needed {needed}"))

            captain = eligible(d, "Boat Captain", BOAT_SHIFT_START, BOAT_SHIFT_END, ignore_max=False)[:1]
            if not captain:
                # Captain must still be assigned when open, even if max hours must be exceeded.
                captain = eligible(d, "Boat Captain", BOAT_SHIFT_START, BOAT_SHIFT_END, ignore_max=True)[:1]
            if captain:
                add_assignment(d, "Boat", BOAT_SHIFT_START, BOAT_SHIFT_END, captain[0], "Boat Captain")
            else:
                violations.append(ViolationOut(date=d.isoformat(), type="role_missing", detail="Missing Boat Captain"))

        if payload.schedule_beach_shop and is_store_open(d) and _is_beach_shop_open(d, season_rules):
            b_start, b_end = payload.hours.beach_shop.start, payload.hours.beach_shop.end
            needed = 2
            added = assign_beach_staff(d, b_start, b_end, needed)
            if added < needed:
                violations.append(ViolationOut(date=d.isoformat(), type="beach_shop_gap", detail=f"Beach Shop needed {needed}"))

        # Ad hoc shifts are bolt-on additions and should not drive baseline staffing.
        apply_ad_hoc_for_day(d)

    # Meet weekly minimums even if that means exceeding baseline daily coverage.
    # Make-up day preference is role-specific (e.g., Team Leader Sat/Fri, Store Clerk Thu/Fri).
    if not payload.shoulder_season:
        for ws in week_starts:
            week_open_days = [
                ws + timedelta(days=i)
                for i in range(7)
                if (ws + timedelta(days=i)) in all_days and is_store_open(ws + timedelta(days=i))
            ]
            if not week_open_days:
                continue
            wk = _week_index(ws, start_date)
            for employee in emp_map.values():
                if weekly_hours[(employee.id, wk)] >= employee.min_hours_per_week:
                    continue
                if requested_days_off_by_week[(employee.id, wk)] > 0:
                    # Respect requested days off by avoiding forced make-up shifts.
                    continue
                makeup_days = _preferred_makeup_days(employee.role, week_open_days)
                location, shift_start, shift_end = _makeup_shift_for(employee.role)
                while weekly_hours[(employee.id, wk)] < employee.min_hours_per_week:
                    added = False
                    for day in makeup_days:
                        if not _can_add_makeup_shift(employee, day, shift_start, shift_end):
                            continue
                        add_assignment(day, location, shift_start, shift_end, employee, employee.role)
                        added = True
                        break
                    if not added:
                        break

    # Final pass: remove avoidable overtime by swapping to eligible same-role staff with remaining capacity.
    rebalance_avoidable_overtime()

    # Validate manager consecutive off rule.
    for ws in week_starts:
        week_days = [ws + timedelta(days=i) for i in range(7) if ws + timedelta(days=i) in all_days]
        for manager_id in manager_ids:
            if any(not is_store_open(d) for d in week_days):
                continue
            work = [manager_id in daily_assigned[d] for d in week_days]
            has_pair = any((not work[i]) and (not work[i + 1]) for i in range(len(work) - 1))
            if payload.leadership_rules.manager_two_consecutive_days_off_per_week and (not payload.shoulder_season) and not has_pair:
                violations.append(ViolationOut(date=ws.isoformat(), type="manager_consecutive_days_off", detail=f"Manager {emp_map[manager_id].name} lacks consecutive days off"))
            requested_days_off = sum(1 for d in week_days if (manager_id, d) in unavail)
            target_days = max(0, (len(week_days) - requested_days_off) if payload.shoulder_season else min(5, len(week_days) - requested_days_off))
            actual_days = sum(work)
            if actual_days < target_days:
                violations.append(ViolationOut(date=ws.isoformat(), type="manager_days_rule", detail=f"Manager {emp_map[manager_id].name} scheduled {actual_days} day(s), minimum is {target_days}"))

    for ws in week_starts:
        wk = _week_index(ws, start_date)
        for e in emp_map.values():
            scheduled_hours = round(weekly_hours[(e.id, wk)], 2)
            if (not payload.shoulder_season) and scheduled_hours < e.min_hours_per_week and requested_days_off_by_week[(e.id, wk)] == 0:
                violations.append(
                    ViolationOut(
                        date=ws.isoformat(),
                        type="hours_min_violation",
                        detail=f"{e.name} scheduled {_format_hours(scheduled_hours)}h, minimum is {e.min_hours_per_week}h",
                    )
                )
            if scheduled_hours > e.max_hours_per_week:
                violations.append(
                    ViolationOut(
                        date=ws.isoformat(),
                        type="hours_max_violation",
                        detail=f"{e.name} scheduled {_format_hours(scheduled_hours)}h, maximum is {e.max_hours_per_week}h",
                    )
                )

    totals: dict[str, TotalsOut] = {e.id: TotalsOut() for e in emp_map.values()}
    for e in emp_map.values():
        totals[e.id].week1_hours = round(weekly_hours[(e.id, 1)], 2)
        totals[e.id].week2_hours = round(weekly_hours[(e.id, 2)], 2)

    daily_presence_by_employee: dict[tuple[str, date], dict[str, bool]] = defaultdict(lambda: {"non_beach": False, "beach": False})
    weekend_days_by_employee: dict[str, set[date]] = defaultdict(set)
    for a in assignments:
        day_presence = daily_presence_by_employee[(a["employee_id"], a["date"])]
        if a["location"] == "Beach Shop":
            day_presence["beach"] = True
        else:
            day_presence["non_beach"] = True

    for (employee_id, work_day), flags in daily_presence_by_employee.items():
        wk = _week_index(work_day, start_date)
        day_credit = 1.0 if flags["non_beach"] else 0.5
        if wk == 1:
            totals[employee_id].week1_days += day_credit
        elif wk == 2:
            totals[employee_id].week2_days += day_credit
        if _is_weekend(work_day):
            weekend_days_by_employee[employee_id].add(work_day)

    for a in assignments:
        totals[a["employee_id"]].locations[a["location"]] += 1

    for e in emp_map.values():
        totals[e.id].week1_days = round(totals[e.id].week1_days, 2)
        totals[e.id].week2_days = round(totals[e.id].week2_days, 2)
        totals[e.id].weekend_days = len(weekend_days_by_employee[e.id])

    out_assignments = [
        AssignmentOut(
            date=a["date"].isoformat(),
            location=a["location"],
            start=a["start"],
            end=a["end"],
            employee_id=a["employee_id"],
            employee_name=a["employee_name"],
            role=a["role"],
            source=a.get("source", "generated"),
        )
        for a in sorted(assignments, key=lambda x: (x["date"], x["location"], x["employee_name"]))
    ]
    return GenerateResponse(assignments=out_assignments, totals_by_employee=totals, violations=sorted(violations, key=lambda v: (v.date, v.type, v.detail)))


def _sample_payload_dict() -> dict:
    today = date.today()
    default_start = _next_sunday_after(today)
    default_rules = _season_rules_for_year(default_start.year)
    return {
        "period": {"start_date": default_start.isoformat(), "weeks": 2},
        "season_rules": {
            "victoria_day": default_rules.victoria_day.isoformat(),
            "june_30": default_rules.june_30.isoformat(),
            "labour_day": default_rules.labour_day.isoformat(),
            "oct_31": default_rules.oct_31.isoformat(),
        },
        "hours": {"greystones": {"start": "08:30", "end": "17:30"}, "beach_shop": {"start": "11:00", "end": "15:00"}},
        "coverage": {"greystones_weekday_staff": 3, "greystones_weekend_staff": 4, "beach_shop_staff": 2},
        "leadership_rules": {"min_team_leaders_every_open_day": 1, "weekend_team_leaders_if_manager_off": 2, "manager_two_consecutive_days_off_per_week": True, "manager_min_weekends_per_month": 2},
        "employees": [
            {"id": "manager_mia", "name": "Manager Mia", "role": "Store Manager", "min_hours_per_week": 24, "max_hours_per_week": 40, "priority_tier": "A", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "taylor", "name": "Taylor", "role": "Team Leader", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "A", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "sam", "name": "Sam", "role": "Team Leader", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "B", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
            {"id": "casey", "name": "Casey", "role": "Boat Captain", "min_hours_per_week": 20, "max_hours_per_week": 40, "priority_tier": "B", "student": False, "availability": {k: [f"{BOAT_SHIFT_START}-{BOAT_SHIFT_END}"] for k in DAY_KEYS}},
            {"id": "jordan", "name": "Jordan", "role": "Store Clerk", "min_hours_per_week": 16, "max_hours_per_week": 40, "priority_tier": "B", "student": False, "availability": {k: ["08:30-17:30"] for k in DAY_KEYS}},
        ],
        "unavailability": [],
        "ad_hoc_bookings": [],
        "history": {"manager_weekends_worked_this_month": 0},
        "open_weekdays": DAY_KEYS,
        "week_start_day": "sun",
        "week_end_day": "sat",
        "reroll_token": 0,
        "schedule_beach_shop": False,
        "shoulder_season": False,
    }

def serialize_employee_record(record: EmployeeRecord) -> Employee:
    return Employee(
        id=record.employee_id,
        name=record.name,
        role=record.role,
        min_hours_per_week=record.min_hours_per_week,
        max_hours_per_week=record.max_hours_per_week,
        priority_tier=record.priority_tier,
        student=record.student,
        availability=record.availability,
    )


def serialize_roster(records: list[EmployeeRecord]) -> list[Employee]:
    return [serialize_employee_record(record) for record in records]


def serialize_schedule_meta(run: ScheduleRun, created_by_email: str) -> ScheduleRunMetaOut:
    return ScheduleRunMetaOut(
        id=run.id,
        created_at=run.created_at,
        created_by_email=created_by_email,
        period_start=run.period_start,
        weeks=run.weeks,
        label=run.label,
    )


def serialize_schedule_out(
    run: ScheduleRun,
    created_by_email: str,
    *,
    day_off_requests: list[DayOffRequestOut] | None = None,
) -> ScheduleRunOut:
    return ScheduleRunOut(
        id=run.id,
        created_at=run.created_at,
        created_by_email=created_by_email,
        period_start=run.period_start,
        weeks=run.weeks,
        label=run.label,
        payload_json=run.payload_json,
        result_json=run.result_json,
        day_off_requests=day_off_requests or [],
    )


def extract_assignments_from_result_json(result_json: Any) -> list[AssignmentOut]:
    if not isinstance(result_json, dict):
        return []
    raw_assignments = result_json.get("assignments")
    if not isinstance(raw_assignments, list):
        return []
    parsed: list[AssignmentOut] = []
    for raw_assignment in raw_assignments:
        try:
            parsed.append(AssignmentOut.model_validate(raw_assignment))
        except Exception:
            continue
    return parsed


def serialize_view_only_schedule(run: ScheduleRun, created_by_email: str) -> ViewOnlyScheduleOut:
    return ViewOnlyScheduleOut(
        id=run.id,
        created_at=run.created_at,
        created_by_email=created_by_email,
        period_start=run.period_start,
        weeks=run.weeks,
        label=run.label,
        assignments=extract_assignments_from_result_json(run.result_json),
    )


def ensure_valid_email(email: str) -> str:
    normalized = normalize_email(email)
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email is required")
    return normalized


@app.post("/auth/bootstrap", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def auth_bootstrap(
    payload: AuthPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
) -> UserOut:
    configured_token = os.getenv("BOOTSTRAP_TOKEN", "")
    if not configured_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Bootstrap token is not configured")
    if bootstrap_token != configured_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid bootstrap token")
    existing_users = db.scalar(select(func.count(User.id))) or 0
    if existing_users > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap is only allowed before the first user exists")
    email = ensure_valid_email(payload.email)
    ensure_password_strength(payload.password)
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        role="admin",
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    session_id = create_session(db, user.id)
    set_session_cookie(response, request, session_id)
    return UserOut.from_orm_user(user)


@app.get("/auth/bootstrap/status", response_model=BootstrapStatusOut)
def auth_bootstrap_status(db: Session = Depends(get_db)) -> BootstrapStatusOut:
    configured_token = bool(os.getenv("BOOTSTRAP_TOKEN", ""))
    if not configured_token:
        return BootstrapStatusOut(enabled=False)
    existing_users = db.scalar(select(func.count(User.id))) or 0
    return BootstrapStatusOut(enabled=existing_users == 0)


@app.post("/auth/login", response_model=UserOut)
def auth_login(
    payload: AuthPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> UserOut:
    email = ensure_valid_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")
    session_id = create_session(db, user.id)
    set_session_cookie(response, request, session_id)
    return UserOut.from_orm_user(user)


@app.post("/auth/logout")
def auth_logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session_if_exists(db, session_id)
    clear_session_cookie(response, request)
    return {"ok": True}


@app.get("/auth/me", response_model=UserOut)
def auth_me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.from_orm_user(current_user)


@app.post("/auth/change-password", response_model=UserOut)
def auth_change_password(
    payload: PasswordChangePayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    ensure_password_strength(payload.new_password)
    if verify_password(payload.new_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from your current password",
        )
    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return UserOut.from_orm_user(current_user)


@app.get("/api/employees", response_model=list[Employee])
def get_employees(
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[Employee]:
    records = db.scalars(select(EmployeeRecord).order_by(EmployeeRecord.sort_order, EmployeeRecord.id)).all()
    return serialize_roster(list(records))


@app.put("/api/employees", response_model=list[Employee])
def put_employees(
    employees: list[Employee] = Body(...),
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[Employee]:
    employee_ids = [employee.id for employee in employees]
    if len(employee_ids) != len(set(employee_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee ids must be unique")
    existing_ids = set(db.scalars(select(EmployeeRecord.employee_id)).all())
    incoming_ids = set(employee_ids)
    removed_ids = existing_ids - incoming_ids
    _unlink_users_for_removed_employee_ids(db, removed_ids)
    db.execute(delete(EmployeeRecord))
    for index, employee in enumerate(employees):
        db.add(
            EmployeeRecord(
                employee_id=employee.id,
                name=employee.name,
                role=employee.role,
                min_hours_per_week=employee.min_hours_per_week,
                max_hours_per_week=employee.max_hours_per_week,
                priority_tier=employee.priority_tier,
                student=employee.student,
                availability=employee.availability,
                sort_order=index,
            )
        )
    db.commit()
    return employees


@app.get("/api/admin/users", response_model=list[UserOut])
def admin_list_users(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    users = db.scalars(select(User).order_by(User.created_at.asc(), User.id.asc())).all()
    return [UserOut.from_orm_user(user) for user in users]


@app.post("/api/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    payload: UserCreatePayload,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> UserOut:
    email = ensure_valid_email(payload.email)
    ensure_password_strength(payload.temporary_password)
    linked_employee_id = normalize_linked_employee_id(payload.linked_employee_id)
    ensure_linked_employee_exists(db, linked_employee_id)
    user = User(
        email=email,
        password_hash=hash_password(payload.temporary_password),
        role=normalize_user_role_input(payload.role),
        linked_employee_id=linked_employee_id,
        is_active=True,
        must_change_password=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise_user_write_error(exc)
    db.refresh(user)
    return UserOut.from_orm_user(user)


@app.patch("/api/admin/users/{user_id}", response_model=UserOut)
def admin_patch_user(
    user_id: int,
    payload: UserPatchPayload,
    current_admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> UserOut:
    provided_fields = set(payload.model_fields_set)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not provided_fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updates were provided")
    if user.id == current_admin.id:
        if payload.role is not None and normalize_user_role_input(payload.role) != "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own role while signed in")
        if payload.is_active is False:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot disable your own account while signed in")
    ensure_active_admin_remains(db, user, payload)
    if "role" in provided_fields:
        if payload.role is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role cannot be null")
        user.role = normalize_user_role_input(payload.role)
    if "is_active" in provided_fields:
        if payload.is_active is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="is_active cannot be null")
        user.is_active = payload.is_active
    if "linked_employee_id" in provided_fields:
        linked_employee_id = normalize_linked_employee_id(payload.linked_employee_id)
        ensure_linked_employee_exists(db, linked_employee_id)
        user.linked_employee_id = linked_employee_id
    if payload.temporary_password:
        ensure_password_strength(payload.temporary_password)
        user.password_hash = hash_password(payload.temporary_password)
        user.must_change_password = True
        db.execute(delete(SessionRecord).where(SessionRecord.user_id == user.id))
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise_user_write_error(exc)
    db.refresh(user)
    return UserOut.from_orm_user(user)


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    current_admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current_admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account while signed in")
    if canonicalize_user_role(user.role) == "admin" and user.is_active:
        active_admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
        if active_admin_count <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one active admin must remain")
    db.delete(user)
    db.commit()
    return {"ok": True}


@app.get("/api/day-off-requests/me", response_model=list[DayOffRequestOut])
def list_my_day_off_requests(
    current_user: User = Depends(get_requesting_user),
    db: Session = Depends(get_db),
) -> list[DayOffRequestOut]:
    rows = db.scalars(
        select(DayOffRequest)
        .where(DayOffRequest.requester_user_id == current_user.id)
        .order_by(DayOffRequest.created_at.desc(), DayOffRequest.id.desc())
    ).all()
    employee_name_by_id = {row.employee_id: row.name for row in db.scalars(select(EmployeeRecord)).all()}
    schedule_ranges = _load_schedule_ranges(db)
    return [
        _serialize_day_off_request(
            row,
            employee_name=employee_name_by_id.get(row.employee_id),
            locked_by_schedule=_first_locked_date_in_range(row.start_date, row.end_date, schedule_ranges) is not None,
        )
        for row in rows
    ]


@app.post("/api/day-off-requests/me", response_model=DayOffRequestOut, status_code=status.HTTP_201_CREATED)
def create_my_day_off_request(
    payload: DayOffRequestCreatePayload,
    current_user: User = Depends(get_requesting_user),
    db: Session = Depends(get_db),
) -> DayOffRequestOut:
    employee_id = ensure_user_has_linked_employee(current_user, db)
    earliest_allowed_date = date.today() + timedelta(days=MIN_DAYS_OFF_REQUEST_NOTICE_DAYS)
    if payload.start_date < earliest_allowed_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Requests must start at least {MIN_DAYS_OFF_REQUEST_NOTICE_DAYS} days in advance (on or after {earliest_allowed_date.isoformat()})",
        )
    first_locked = _find_first_scheduled_date_in_range(db, payload.start_date, payload.end_date)
    if first_locked is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot request day(s) off for {first_locked.isoformat()} because a schedule for that date already exists",
        )
    overlapping_existing = db.scalar(
        select(DayOffRequest.id).where(
            DayOffRequest.employee_id == employee_id,
            DayOffRequest.status.in_(["pending", "approved"]),
            DayOffRequest.start_date <= payload.end_date,
            DayOffRequest.end_date >= payload.start_date,
        )
    )
    if overlapping_existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This request overlaps an existing pending or approved request")

    request_row = DayOffRequest(
        requester_user_id=current_user.id,
        employee_id=employee_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        request_reason=(payload.reason or "").strip(),
        status="pending",
    )
    db.add(request_row)
    db.commit()
    db.refresh(request_row)
    employee_name = db.scalar(select(EmployeeRecord.name).where(EmployeeRecord.employee_id == request_row.employee_id))
    return _serialize_day_off_request(request_row, employee_name=employee_name, locked_by_schedule=False)


@app.post("/api/day-off-requests/me/{request_id}/cancel", response_model=DayOffRequestOut)
def cancel_my_day_off_request(
    request_id: int,
    payload: DayOffRequestCancelPayload,
    current_user: User = Depends(get_requesting_user),
    db: Session = Depends(get_db),
) -> DayOffRequestOut:
    request_row = db.scalar(
        select(DayOffRequest).where(
            DayOffRequest.id == request_id,
            DayOffRequest.requester_user_id == current_user.id,
        )
    )
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Day-off request not found")
    if request_row.status == "pending":
        pass
    elif request_row.status == "approved":
        if _request_is_locked_by_schedule(db, request_row):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approved requests cannot be cancelled after a schedule exists for those dates")
    else:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only pending or approved requests can be cancelled")

    request_row.status = "cancelled"
    request_row.cancelled_reason = (payload.reason or "").strip() or None
    request_row.cancelled_by_user_id = current_user.id
    request_row.cancelled_by_role = canonicalize_user_role(current_user.role)
    request_row.cancelled_at = utcnow()
    db.add(request_row)
    db.commit()
    db.refresh(request_row)
    employee_name = db.scalar(select(EmployeeRecord.name).where(EmployeeRecord.employee_id == request_row.employee_id))
    return _serialize_day_off_request(
        request_row,
        employee_name=employee_name,
        locked_by_schedule=_request_is_locked_by_schedule(db, request_row),
    )


@app.get("/api/day-off-requests/approved", response_model=list[ApprovedDayOffEntryOut])
def list_approved_day_off_entries(
    start_date: date,
    end_date: date,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[ApprovedDayOffEntryOut]:
    if end_date < start_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
    return _approved_day_off_entries_for_range(db, start_date=start_date, end_date=end_date)


@app.get("/api/admin/day-off-requests", response_model=list[DayOffRequestOut])
def admin_list_day_off_requests(
    status_filter: DayOffRequestStatus | None = None,
    include_past: bool = True,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[DayOffRequestOut]:
    stmt = select(DayOffRequest)
    if status_filter is not None:
        if status_filter not in DAY_OFF_STATUS_VALUES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status_filter")
        stmt = stmt.where(DayOffRequest.status == status_filter)
    if not include_past:
        stmt = stmt.where(DayOffRequest.end_date >= date.today())
    rows = db.scalars(stmt.order_by(DayOffRequest.start_date.asc(), DayOffRequest.created_at.desc(), DayOffRequest.id.desc())).all()
    requester_email_by_id = {row.id: row.email for row in db.scalars(select(User)).all()}
    employee_name_by_id = {row.employee_id: row.name for row in db.scalars(select(EmployeeRecord)).all()}
    schedule_ranges = _load_schedule_ranges(db)
    return [
        _serialize_day_off_request(
            row,
            requester_email=requester_email_by_id.get(row.requester_user_id),
            employee_name=employee_name_by_id.get(row.employee_id),
            locked_by_schedule=_first_locked_date_in_range(row.start_date, row.end_date, schedule_ranges) is not None,
        )
        for row in rows
    ]


@app.delete("/api/admin/day-off-requests/previous")
def admin_delete_previous_day_off_requests(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, int | bool]:
    schedule_ranges = _load_schedule_ranges(db)
    if not schedule_ranges:
        return {"ok": True, "deleted": 0}

    request_ids_to_delete = [
        row.id
        for row in db.scalars(select(DayOffRequest)).all()
        if _first_locked_date_in_range(row.start_date, row.end_date, schedule_ranges) is not None
    ]
    if not request_ids_to_delete:
        return {"ok": True, "deleted": 0}

    db.execute(delete(DayOffRequest).where(DayOffRequest.id.in_(request_ids_to_delete)))
    db.commit()
    return {"ok": True, "deleted": len(request_ids_to_delete)}


@app.post("/api/admin/day-off-requests/{request_id}/decision", response_model=DayOffRequestOut)
def admin_decide_day_off_request(
    request_id: int,
    payload: DayOffRequestDecisionPayload,
    current_admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> DayOffRequestOut:
    request_row = db.get(DayOffRequest, request_id)
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Day-off request not found")
    if request_row.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cancelled requests cannot be updated")

    reason = (payload.reason or "").strip()
    locked_by_schedule = _request_is_locked_by_schedule(db, request_row)
    if payload.action == "approve":
        if locked_by_schedule:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Requests cannot be approved after a schedule exists for those dates")
        overlapping_existing = db.scalar(
            select(DayOffRequest.id).where(
                DayOffRequest.id != request_row.id,
                DayOffRequest.employee_id == request_row.employee_id,
                DayOffRequest.status.in_(["pending", "approved"]),
                DayOffRequest.start_date <= request_row.end_date,
                DayOffRequest.end_date >= request_row.start_date,
            )
        )
        if overlapping_existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This request overlaps an existing pending or approved request")
        request_row.status = "approved"
        request_row.decision_reason = reason or None
    else:
        if request_row.status == "approved" and locked_by_schedule:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approved requests cannot be reversed after a schedule exists for those dates")
        if not reason:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A reason is required when rejecting a request")
        request_row.status = "rejected"
        request_row.decision_reason = reason

    request_row.decided_by_user_id = current_admin.id
    request_row.decided_at = utcnow()
    db.add(request_row)
    db.commit()
    db.refresh(request_row)
    requester_email = db.scalar(select(User.email).where(User.id == request_row.requester_user_id))
    employee_name = db.scalar(select(EmployeeRecord.name).where(EmployeeRecord.employee_id == request_row.employee_id))
    return _serialize_day_off_request(
        request_row,
        requester_email=requester_email,
        employee_name=employee_name,
        locked_by_schedule=_request_is_locked_by_schedule(db, request_row),
    )


@app.post("/api/admin/day-off-requests/{request_id}/cancel", response_model=DayOffRequestOut)
def admin_cancel_approved_day_off_request(
    request_id: int,
    payload: DayOffRequestAdminCancelPayload,
    current_admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> DayOffRequestOut:
    request_row = db.get(DayOffRequest, request_id)
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Day-off request not found")
    if request_row.status != "approved":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only approved requests can be cancelled by administrators")
    if _request_is_locked_by_schedule(db, request_row):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Approved requests cannot be cancelled after a schedule exists for those dates")
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A reason is required when cancelling an approved request")

    request_row.status = "cancelled"
    request_row.cancelled_reason = reason
    request_row.cancelled_by_user_id = current_admin.id
    request_row.cancelled_by_role = "admin"
    request_row.cancelled_at = utcnow()
    db.add(request_row)
    db.commit()
    db.refresh(request_row)
    requester_email = db.scalar(select(User.email).where(User.id == request_row.requester_user_id))
    employee_name = db.scalar(select(EmployeeRecord.name).where(EmployeeRecord.employee_id == request_row.employee_id))
    return _serialize_day_off_request(
        request_row,
        requester_email=requester_email,
        employee_name=employee_name,
        locked_by_schedule=False,
    )


@app.get("/api/schedules", response_model=list[ScheduleRunMetaOut])
def list_schedules(
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[ScheduleRunMetaOut]:
    rows = db.execute(
        select(ScheduleRun, User.email)
        .join(User, ScheduleRun.created_by_user_id == User.id)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
    ).all()
    return [serialize_schedule_meta(run, email) for run, email in rows]


@app.post("/api/schedules", response_model=ScheduleRunMetaOut, status_code=status.HTTP_201_CREATED)
def create_schedule(
    payload: ScheduleSavePayload,
    current_user: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> ScheduleRunMetaOut:
    schedule_run = ScheduleRun(
        created_by_user_id=current_user.id,
        period_start=payload.period_start,
        weeks=payload.weeks,
        label=payload.label,
        payload_json=payload.payload_json,
        result_json=payload.result_json,
    )
    db.add(schedule_run)
    db.commit()
    db.refresh(schedule_run)
    return serialize_schedule_meta(schedule_run, current_user.email)


@app.get("/api/schedules/{schedule_id}", response_model=ScheduleRunOut)
def get_schedule(
    schedule_id: int,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> ScheduleRunOut:
    row = db.execute(
        select(ScheduleRun, User.email)
        .join(User, ScheduleRun.created_by_user_id == User.id)
        .where(ScheduleRun.id == schedule_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved schedule not found")
    run, email = row
    schedule_end = _schedule_run_end_date(run.period_start, run.weeks)
    day_off_requests = _day_off_requests_for_range(
        db,
        start_date=run.period_start,
        end_date=schedule_end,
        statuses={"approved"},
    )
    return serialize_schedule_out(run, email, day_off_requests=day_off_requests)


@app.get("/api/view-only/schedules", response_model=list[ViewOnlyScheduleOut])
def list_view_only_schedules(
    _: User = Depends(get_view_only_user),
    db: Session = Depends(get_db),
) -> list[ViewOnlyScheduleOut]:
    rows = db.execute(
        select(ScheduleRun, User.email)
        .join(User, ScheduleRun.created_by_user_id == User.id)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id.desc())
        .limit(2)
    ).all()
    return [serialize_view_only_schedule(run, email) for run, email in rows]


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    schedule_run = db.get(ScheduleRun, schedule_id)
    if schedule_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved schedule not found")
    db.delete(schedule_run)
    db.commit()
    return {"ok": True}


@app.delete("/api/schedules")
def delete_all_schedules(
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, int | bool]:
    result = db.execute(delete(ScheduleRun))
    db.commit()
    deleted_count = int(result.rowcount or 0)
    return {"ok": True, "deleted": deleted_count}


@app.post("/settings")
def update_settings_compat():
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "env": os.getenv("ENVIRONMENT", "local")}


@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    current_user = get_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))
    if (
        current_user is not None
        and canonicalize_user_role(current_user.role) == "view_only"
        and not current_user.must_change_password
    ):
        return RedirectResponse(url="/viewer", status_code=status.HTTP_303_SEE_OTHER)
    payload_json = json.dumps(_sample_payload_dict())
    return templates.TemplateResponse(request, "pages/index.html", {"request": request, "payload_json": payload_json})


@app.get("/viewer")
def view_only_dashboard(request: Request, db: Session = Depends(get_db)):
    current_user = get_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))
    if current_user is None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    if current_user.must_change_password:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    if canonicalize_user_role(current_user.role) != "view_only":
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "pages/view_only.html", {"request": request})


@app.get("/time-clock")
def time_clock_dashboard(request: Request, db: Session = Depends(get_db)):
    current_user = get_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))
    if current_user is None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    if current_user.must_change_password:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    if canonicalize_user_role(current_user.role) not in MANAGER_OR_ADMIN_ROLES:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    today_local = local_now().date().isoformat()
    return templates.TemplateResponse(
        request,
        "pages/time_clock.html",
        {
            "request": request,
            "policy_json": json.dumps(build_time_clock_policy().model_dump()),
            "today_local": today_local,
        },
    )


@app.get("/kiosk")
def kiosk_page(request: Request):
    return templates.TemplateResponse(
        request,
        "pages/kiosk.html",
        {
            "request": request,
            "policy_json": json.dumps(build_time_clock_policy().model_dump()),
        },
    )


@app.get("/api/time-clock/policy", response_model=TimeClockPolicyOut)
def get_time_clock_policy() -> TimeClockPolicyOut:
    return build_time_clock_policy()


@app.get("/api/time-clock/staff", response_model=list[TimeClockStaffOut])
def list_time_clock_staff(
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[TimeClockStaffOut]:
    employee_by_id = {row.employee_id: row for row in db.scalars(select(EmployeeRecord)).all()}
    users = db.scalars(
        select(User)
        .where(User.linked_employee_id.is_not(None))
        .order_by(User.email.asc(), User.id.asc())
    ).all()
    staff_rows: list[TimeClockStaffOut] = []
    for user in users:
        linked_employee_id = user.linked_employee_id or ""
        employee = employee_by_id.get(linked_employee_id)
        if employee is None:
            continue
        staff_rows.append(_time_clock_staff_out(user, employee))
    staff_rows.sort(key=lambda row: ((row.employee_name or "").lower(), (row.email or "").lower(), row.linked_employee_id))
    return staff_rows


@app.post("/api/time-clock/staff/{user_id}/pin", response_model=TimeClockStaffOut)
def set_time_clock_pin(
    user_id: int,
    payload: ClockPinPayload,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> TimeClockStaffOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    linked_employee_id = normalize_linked_employee_id(user.linked_employee_id)
    if linked_employee_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not linked to an employee")
    ensure_linked_employee_exists(db, linked_employee_id)
    normalized_pin = ensure_clock_pin_strength(payload.pin)
    lookup = pin_lookup_key(normalized_pin)
    existing = db.scalar(select(User).where(User.clock_pin_lookup == lookup, User.id != user.id))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That PIN is already assigned to another employee")
    _set_user_clock_pin(user, normalized_pin, temporary=payload.temporary)
    db.add(user)
    db.commit()
    db.refresh(user)
    employee = db.scalar(select(EmployeeRecord).where(EmployeeRecord.employee_id == linked_employee_id))
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee was not found")
    return _time_clock_staff_out(user, employee)


@app.delete("/api/time-clock/staff/{user_id}/pin", response_model=TimeClockStaffOut)
def disable_time_clock_pin(
    user_id: int,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> TimeClockStaffOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    linked_employee_id = normalize_linked_employee_id(user.linked_employee_id)
    if linked_employee_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not linked to an employee")
    user.clock_pin_hash = None
    user.clock_pin_lookup = None
    user.clock_pin_enabled = False
    user.clock_pin_temporary = False
    user.clock_pin_updated_at = utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    employee = db.scalar(select(EmployeeRecord).where(EmployeeRecord.employee_id == linked_employee_id))
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee was not found")
    return _time_clock_staff_out(user, employee)


@app.delete("/api/time-clock/staff/{employee_id}")
def delete_time_clock_staff(
    employee_id: str,
    current_user: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    normalized_employee_id = (employee_id or "").strip()
    if not normalized_employee_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee id is required")
    employee = db.scalar(select(EmployeeRecord).where(EmployeeRecord.employee_id == normalized_employee_id))
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    open_records = db.scalar(
        select(func.count(AttendanceRecord.id)).where(
            AttendanceRecord.employee_id == normalized_employee_id,
            AttendanceRecord.status == "open",
        )
    ) or 0
    if open_records:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Close the employee's open attendance record before deleting them")

    linked_users = db.scalars(select(User).where(User.linked_employee_id == normalized_employee_id)).all()
    for user in linked_users:
        if user.id == current_user.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own employee record while signed in")
        if canonicalize_user_role(user.role) == "admin" and user.is_active:
            active_admin_count = db.scalar(select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))) or 0
            if active_admin_count <= 1:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one active admin must remain")

    _unlink_users_for_removed_employee_ids(db, {normalized_employee_id})
    db.delete(employee)
    db.commit()
    return {"ok": True}


@app.get("/api/time-clock/records", response_model=list[AttendanceRecordOut])
def list_time_clock_records(
    start_date: date | None = None,
    end_date: date | None = None,
    review_state: TimeClockReviewState | None = None,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> list[AttendanceRecordOut]:
    current_local = local_now()
    today_local = current_local.date()
    query_start = start_date or (today_local - timedelta(days=13))
    query_end = end_date or today_local
    if query_end < query_start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
    _auto_close_captain_records(db, current_local=current_local, include_current_day=True)
    stmt = select(AttendanceRecord).where(
        AttendanceRecord.work_date >= query_start,
        AttendanceRecord.work_date <= query_end,
    )
    if review_state is not None:
        stmt = stmt.where(AttendanceRecord.review_state == review_state)
    rows = db.scalars(stmt.order_by(AttendanceRecord.work_date.desc(), AttendanceRecord.id.desc())).all()
    return [_serialize_attendance_record(row) for row in rows]


@app.patch("/api/time-clock/records/{record_id}", response_model=AttendanceRecordOut)
def patch_time_clock_record(
    record_id: int,
    payload: AttendanceRecordPatchPayload,
    current_user: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> AttendanceRecordOut:
    record = db.get(AttendanceRecord, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")
    provided_fields = set(payload.model_fields_set)
    if not provided_fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updates were provided")
    previous_in = record.effective_clock_in_at
    previous_out = record.effective_clock_out_at
    times_changed = False

    if "effective_clock_in_local" in provided_fields:
        if payload.effective_clock_in_local is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="effective_clock_in_local cannot be null")
        record.effective_clock_in_at = local_datetime_to_utc(build_local_datetime(record.work_date, payload.effective_clock_in_local))
        times_changed = True
    if "effective_clock_out_local" in provided_fields:
        if payload.effective_clock_out_local is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="effective_clock_out_local cannot be null")
        record.effective_clock_out_at = local_datetime_to_utc(build_local_datetime(record.work_date, payload.effective_clock_out_local))
        record.actual_clock_out_at = record.actual_clock_out_at or record.effective_clock_out_at
        record.status = "closed"
        times_changed = True

    if times_changed and not (payload.reason or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A reason is required when editing attendance times")
    if record.effective_clock_out_at is not None and record.effective_clock_out_at <= record.effective_clock_in_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Clock-out time must be after clock-in time")

    if times_changed:
        _log_attendance_adjustment(
            db,
            record=record,
            requested_by_user_id=current_user.id,
            action="manager_edit",
            reason=payload.reason,
            previous_in=previous_in,
            previous_out=previous_out,
            new_in=record.effective_clock_in_at,
            new_out=record.effective_clock_out_at,
        )
        if record.effective_clock_out_at is not None:
            _recalculate_attendance_record(record)
        record.review_note = (payload.reason or "").strip() or record.review_note
        record.review_state = payload.mark_review_state or "approved"
    elif payload.mark_review_state is not None:
        record.review_state = payload.mark_review_state
        if (payload.reason or "").strip():
            record.review_note = (payload.reason or "").strip()

    db.add(record)
    db.commit()
    db.refresh(record)
    return _serialize_attendance_record(record)


@app.post("/api/time-clock/records/{record_id}/approve", response_model=AttendanceRecordOut)
def approve_time_clock_record(
    record_id: int,
    payload: AttendanceApprovePayload,
    current_user: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> AttendanceRecordOut:
    record = db.get(AttendanceRecord, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")
    _log_attendance_adjustment(
        db,
        record=record,
        requested_by_user_id=current_user.id,
        action="manager_approve",
        reason=payload.note,
        previous_in=record.effective_clock_in_at,
        previous_out=record.effective_clock_out_at,
        new_in=record.effective_clock_in_at,
        new_out=record.effective_clock_out_at,
    )
    record.review_state = "approved"
    if (payload.note or "").strip():
        record.review_note = (payload.note or "").strip()
    db.add(record)
    db.commit()
    db.refresh(record)
    return _serialize_attendance_record(record)


@app.get("/api/time-clock/export.csv")
def export_time_clock_csv(
    start_date: date | None = None,
    end_date: date | None = None,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    current_local = local_now()
    today_local = current_local.date()
    query_start = start_date or (today_local - timedelta(days=13))
    query_end = end_date or today_local
    if query_end < query_start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
    _auto_close_captain_records(db, current_local=current_local, include_current_day=True)
    rows = db.scalars(
        select(AttendanceRecord)
        .where(AttendanceRecord.work_date >= query_start, AttendanceRecord.work_date <= query_end)
        .order_by(AttendanceRecord.work_date.asc(), AttendanceRecord.employee_name_snapshot.asc(), AttendanceRecord.id.asc())
    ).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "work_date",
            "employee_name",
            "role",
            "scheduled_start",
            "scheduled_end",
            "scheduled_paid_hours",
            "effective_clock_in",
            "effective_clock_out",
            "payable_hours",
            "break_deduction_minutes",
            "review_state",
            "review_note",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.work_date.isoformat(),
                row.employee_name_snapshot,
                row.role_snapshot or "",
                format_minutes_as_clock(row.scheduled_start_minutes) or "",
                format_minutes_as_clock(row.scheduled_end_minutes) or "",
                _hours_value_from_minutes(row.scheduled_paid_minutes) or "",
                format_local_time(row.effective_clock_in_at) or "",
                format_local_time(row.effective_clock_out_at) or "",
                _hours_value_from_minutes(row.payable_minutes) or "",
                row.break_deduction_minutes or "",
                row.review_state,
                row.review_note or "",
            ]
        )
    return Response(content=out.getvalue(), media_type="text/csv")


@app.get("/api/time-clock/timesheet", response_model=TimesheetOut)
def get_time_clock_timesheet(
    start_date: date | None = None,
    end_date: date | None = None,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> TimesheetOut:
    current_local = local_now()
    today_local = current_local.date()
    query_start = start_date or (today_local - timedelta(days=13))
    query_end = end_date or today_local
    if query_end < query_start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
    _auto_close_captain_records(db, current_local=current_local, include_current_day=True)
    rows = db.scalars(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.work_date >= query_start,
            AttendanceRecord.work_date <= query_end,
            AttendanceRecord.status == "closed",
        )
        .order_by(AttendanceRecord.work_date.asc(), AttendanceRecord.employee_name_snapshot.asc(), AttendanceRecord.id.asc())
    ).all()
    return _build_timesheet(rows, start_date=query_start, end_date=query_end)


@app.get("/api/time-clock/timesheet.csv")
def export_time_clock_timesheet_csv(
    start_date: date | None = None,
    end_date: date | None = None,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    current_local = local_now()
    today_local = current_local.date()
    query_start = start_date or (today_local - timedelta(days=13))
    query_end = end_date or today_local
    if query_end < query_start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end_date must be on or after start_date")
    _auto_close_captain_records(db, current_local=current_local, include_current_day=True)
    rows = db.scalars(
        select(AttendanceRecord)
        .where(
            AttendanceRecord.work_date >= query_start,
            AttendanceRecord.work_date <= query_end,
            AttendanceRecord.status == "closed",
        )
        .order_by(AttendanceRecord.work_date.asc(), AttendanceRecord.employee_name_snapshot.asc(), AttendanceRecord.id.asc())
    ).all()
    timesheet = _build_timesheet(rows, start_date=query_start, end_date=query_end)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "work_date",
            "employee_name",
            "role",
            "first_in",
            "last_out",
            "payable_hours",
            "break_deduction_minutes",
            "shift_count",
            "exception_count",
        ]
    )
    for row in timesheet.rows:
        writer.writerow(
            [
                row.work_date.isoformat(),
                row.employee_name,
                row.role or "",
                row.first_in_local or "",
                row.last_out_local or "",
                row.payable_hours,
                row.break_deduction_minutes,
                row.shift_count,
                row.exception_count,
            ]
        )
    writer.writerow([])
    writer.writerow(["employee_name", "role", "worked_days", "payable_hours"])
    for total in timesheet.employee_totals:
        writer.writerow(
            [
                total.employee_name,
                total.role or "",
                total.worked_days,
                total.payable_hours,
            ]
        )
    writer.writerow([])
    writer.writerow(["period_start", timesheet.start_date.isoformat()])
    writer.writerow(["period_end", timesheet.end_date.isoformat()])
    writer.writerow(["grand_total_hours", timesheet.grand_total_hours])
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="timesheet-{timesheet.start_date.isoformat()}-{timesheet.end_date.isoformat()}.csv"'
        },
    )


@app.post("/api/kiosk/unlock", response_model=KioskStatusOut)
def kiosk_unlock(
    payload: KioskUnlockPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> KioskStatusOut:
    email = ensure_valid_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")
    if user.must_change_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account must change its password before it can unlock a kiosk")
    if canonicalize_user_role(user.role) not in MANAGER_OR_ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manager or admin access required")
    session_id = create_kiosk_session(db, user.id, payload.session_label)
    set_kiosk_session_cookie(response, request, session_id)
    session = db.get(KioskSession, session_id)
    return KioskStatusOut(
        unlocked=True,
        unlocked_by_email=user.email,
        expires_at=session.expires_at if session is not None else utcnow() + timedelta(seconds=KIOSK_SESSION_MAX_AGE_SECONDS),
        session_label=(payload.session_label or "").strip() or None,
    )


@app.post("/api/kiosk/lock", response_model=KioskStatusOut)
def kiosk_lock(
    payload: KioskLockPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> KioskStatusOut:
    email = ensure_valid_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")
    if canonicalize_user_role(user.role) not in MANAGER_OR_ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manager or admin access required")
    delete_kiosk_session_if_exists(db, request.cookies.get(KIOSK_SESSION_COOKIE_NAME))
    clear_kiosk_session_cookie(response, request)
    return KioskStatusOut(unlocked=False)


@app.get("/api/kiosk/status", response_model=KioskStatusOut)
def kiosk_status(
    request: Request,
    db: Session = Depends(get_db),
) -> KioskStatusOut:
    session, user = get_kiosk_session_state(db, request.cookies.get(KIOSK_SESSION_COOKIE_NAME))
    if session is None or user is None:
        return KioskStatusOut(unlocked=False)
    return KioskStatusOut(
        unlocked=True,
        unlocked_by_email=user.email,
        expires_at=session.expires_at,
        session_label=session.session_label,
    )


@app.post("/api/kiosk/clock", response_model=KioskClockResponse)
def kiosk_clock(
    payload: KioskClockPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> KioskClockResponse:
    get_active_kiosk_session(request, db)
    user = _find_kiosk_user_by_pin(db, payload.pin)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid PIN")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Employee account is disabled")
    linked_employee_id = normalize_linked_employee_id(user.linked_employee_id)
    if linked_employee_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This employee account is not linked to the roster")
    ensure_linked_employee_exists(db, linked_employee_id)

    employee_record = db.scalar(select(EmployeeRecord).where(EmployeeRecord.employee_id == linked_employee_id))
    employee_name = employee_record.name if employee_record is not None else linked_employee_id
    employee_role = employee_record.role if employee_record is not None else None
    if user.clock_pin_temporary:
        if not payload.new_pin or not payload.confirm_new_pin:
            return KioskClockResponse(
                action="pin_change_required",
                message="Temporary PIN accepted. Set a new PIN to continue.",
                record=None,
                employee_name=employee_name,
            )
        if payload.new_pin != payload.confirm_new_pin:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New PIN entries do not match")
        normalized_new_pin = ensure_clock_pin_strength(payload.new_pin)
        existing = db.scalar(select(User).where(User.clock_pin_lookup == pin_lookup_key(normalized_new_pin), User.id != user.id))
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That PIN is already assigned to another employee")
        _set_user_clock_pin(user, normalized_new_pin, temporary=False)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif payload.new_pin or payload.confirm_new_pin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PIN changes are only available when using a temporary PIN")

    actual_action_time = utcnow()
    actual_action_time_local = utc_to_local(actual_action_time)
    if actual_action_time_local is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to determine local time")
    _auto_close_captain_records(
        db,
        current_local=actual_action_time_local,
        include_current_day=False,
        user_id=user.id,
    )

    open_record = db.scalars(
        select(AttendanceRecord)
        .where(AttendanceRecord.user_id == user.id, AttendanceRecord.status == "open")
        .order_by(AttendanceRecord.created_at.desc(), AttendanceRecord.id.desc())
    ).first()

    override_reason = (payload.override_reason or "").strip()
    override_time_value = (payload.override_time or "").strip() or None
    if override_time_value and not override_reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A reason is required when manually adjusting a punch time")

    if open_record is None:
        work_date = actual_action_time_local.date()
        effective_clock_in_local = actual_action_time_local
        review_notes: list[str] = []
        informational_note: str | None = None
        if override_time_value is not None:
            override_local = build_local_datetime(work_date, override_time_value)
            if override_local > actual_action_time_local + timedelta(minutes=5):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Clock-in time cannot be set in the future")
            override_delta_minutes = abs(span_minutes(override_local, actual_action_time_local))
            if override_delta_minutes > MAX_SELF_SERVICE_ADJUSTMENT_MINUTES:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Clock-in adjustment exceeds the kiosk limit")
            if override_delta_minutes > AUTO_APPROVE_ADJUSTMENT_MINUTES:
                review_notes.append("Manual clock-in adjustment exceeded the auto-approval window")
            effective_clock_in_local = override_local
        effective_clock_in_local = normalize_captain_clock_in(effective_clock_in_local, employee_role)
        effective_clock_in_at = local_datetime_to_utc(effective_clock_in_local)
        scheduled_shift = _load_effective_scheduled_shift_for_employee(
            db,
            employee_id=linked_employee_id,
            employee_role=employee_role,
            work_date=work_date,
        )
        if scheduled_shift is None:
            informational_note = "No saved schedule matched this punch. Hours will be calculated from the approved punch times."
        review_state, review_note = _record_review_state(review_notes)
        if review_note is None and informational_note is not None:
            review_note = informational_note
        record = AttendanceRecord(
            user_id=user.id,
            employee_id=linked_employee_id,
            employee_name_snapshot=employee_name,
            role_snapshot=employee_role,
            work_date=work_date,
            schedule_run_id=int(scheduled_shift["schedule_run_id"]) if scheduled_shift is not None and scheduled_shift["schedule_run_id"] is not None else None,
            scheduled_start_minutes=int(scheduled_shift["scheduled_start_minutes"]) if scheduled_shift is not None and scheduled_shift["scheduled_start_minutes"] is not None else None,
            scheduled_end_minutes=int(scheduled_shift["scheduled_end_minutes"]) if scheduled_shift is not None and scheduled_shift["scheduled_end_minutes"] is not None else None,
            scheduled_paid_minutes=int(scheduled_shift["scheduled_paid_minutes"]) if scheduled_shift is not None and scheduled_shift["scheduled_paid_minutes"] is not None else None,
            actual_clock_in_at=actual_action_time,
            effective_clock_in_at=effective_clock_in_at,
            status="open",
            review_state=review_state,
            review_note=review_note,
            last_action_source="kiosk",
        )
        db.add(record)
        db.flush()
        if override_time_value is not None:
            _log_attendance_adjustment(
                db,
                record=record,
                requested_by_user_id=user.id,
                action="clock_in_override",
                reason=override_reason,
                previous_in=actual_action_time,
                previous_out=None,
                new_in=effective_clock_in_at,
                new_out=None,
            )
        db.commit()
        db.refresh(record)
        return KioskClockResponse(
            action="clocked_in",
            message=f"{employee_name} clocked in",
            record=_serialize_attendance_record(record),
            employee_name=employee_name,
        )

    effective_clock_out_local = actual_action_time_local
    review_notes = [open_record.review_note] if open_record.review_state == "needs_review" and open_record.review_note else []
    informational_note = open_record.review_note if open_record.review_state == "clear" and open_record.review_note else None
    if override_time_value is not None:
        override_local = build_local_datetime(open_record.work_date, override_time_value)
        if override_local > actual_action_time_local + timedelta(minutes=5):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Clock-out time cannot be set in the future")
        override_delta_minutes = abs(span_minutes(override_local, actual_action_time_local))
        if override_delta_minutes > MAX_SELF_SERVICE_ADJUSTMENT_MINUTES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Clock-out adjustment exceeds the kiosk limit")
        if override_delta_minutes > AUTO_APPROVE_ADJUSTMENT_MINUTES:
            review_notes.append("Manual clock-out adjustment exceeded the auto-approval window")
        effective_clock_out_local = override_local
    effective_clock_out_local = normalize_captain_clock_out(effective_clock_out_local, employee_role)
    if effective_clock_out_local <= (utc_to_local(open_record.effective_clock_in_at) or actual_action_time_local):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Clock-out time must be after clock-in time")
    effective_clock_out_at = local_datetime_to_utc(effective_clock_out_local)

    open_record.actual_clock_out_at = actual_action_time
    open_record.effective_clock_out_at = effective_clock_out_at
    open_record.status = "closed"
    open_record.last_action_source = "kiosk"
    _recalculate_attendance_record(open_record)

    effective_in_local = utc_to_local(open_record.effective_clock_in_at)
    effective_out_local = utc_to_local(open_record.effective_clock_out_at)
    if effective_in_local is None or effective_out_local is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to determine local work span")
    review_state, review_note = _record_review_state(review_notes, current=open_record.review_state)
    if review_note is None and informational_note is not None:
        review_note = informational_note
    open_record.review_state = review_state
    open_record.review_note = review_note
    db.add(open_record)
    if override_time_value is not None:
        _log_attendance_adjustment(
            db,
            record=open_record,
            requested_by_user_id=user.id,
            action="clock_out_override",
            reason=override_reason,
            previous_in=open_record.effective_clock_in_at,
            previous_out=actual_action_time,
            new_in=open_record.effective_clock_in_at,
            new_out=effective_clock_out_at,
        )
    db.commit()
    db.refresh(open_record)
    return KioskClockResponse(
        action="clocked_out",
        message=f"{employee_name} clocked out",
        record=_serialize_attendance_record(open_record),
        employee_name=employee_name,
    )


@app.post("/generate", response_model=GenerateResponse)
def generate(
    payload: GenerateRequest,
    _: User = Depends(get_manager_or_admin_user),
    db: Session = Depends(get_db),
) -> GenerateResponse:
    if payload.period.start_date < date.today():
        raise HTTPException(status_code=400, detail="Start date cannot be in the past")
    schedule_start = _next_or_same_day(payload.period.start_date, payload.week_start_day)
    schedule_end = schedule_start + timedelta(days=(payload.period.weeks * 7) - 1)
    approved_entries = _approved_day_off_entries_for_range(db, start_date=schedule_start, end_date=schedule_end)
    payload.unavailability = _merge_unavailability_with_approved_day_off(payload, approved_entries)
    history_weekly_hours, history_weekly_leader_days, history_weekly_work_days = _load_generation_history_maps(db, payload)
    return _generate(
        payload,
        history_weekly_hours=history_weekly_hours,
        history_weekly_leader_days=history_weekly_leader_days,
        history_weekly_work_days=history_weekly_work_days,
    )
