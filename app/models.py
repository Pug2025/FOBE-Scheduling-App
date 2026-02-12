from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Settings(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    season: Mapped[str] = mapped_column(String(20), default="school")
    start_date: Mapped[date] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(Integer, default=14)
    manager_consecutive_days_off: Mapped[int] = mapped_column(Integer, default=2)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    role: Mapped[str] = mapped_column(String(20), default="staff")
    leadership_score: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    availability = relationship("Availability", back_populates="employee", cascade="all, delete-orphan")
    time_off = relationship("TimeOff", back_populates="employee", cascade="all, delete-orphan")


class Availability(Base):
    __tablename__ = "availability"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    day_of_week: Mapped[int] = mapped_column(Integer)  # 0-6
    block: Mapped[str] = mapped_column(String(20))  # morning/evening
    available: Mapped[bool] = mapped_column(Boolean, default=True)

    employee = relationship("Employee", back_populates="availability")
    __table_args__ = (UniqueConstraint("employee_id", "day_of_week", "block", name="uq_employee_block"),)


class TimeOff(Base):
    __tablename__ = "time_off"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    date: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(String(200), default="")

    employee = relationship("Employee", back_populates="time_off")
    __table_args__ = (UniqueConstraint("employee_id", "date", name="uq_employee_date_off"),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default="generated")
    seed: Mapped[int] = mapped_column(Integer, default=0)

    assignments = relationship("Assignment", back_populates="run", cascade="all, delete-orphan")
    violations = relationship("Violation", back_populates="run", cascade="all, delete-orphan")


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    date: Mapped[date] = mapped_column(Date)
    block: Mapped[str] = mapped_column(String(20))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    run = relationship("Run", back_populates="assignments")
    employee = relationship("Employee")
    __table_args__ = (UniqueConstraint("run_id", "date", "block", name="uq_run_block"),)


class Violation(Base):
    __tablename__ = "violations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    date: Mapped[date] = mapped_column(Date)
    severity: Mapped[str] = mapped_column(String(20), default="soft")
    message: Mapped[str] = mapped_column(Text)

    run = relationship("Run", back_populates="violations")
