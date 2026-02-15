from datetime import date

import pytest
from pydantic import ValidationError

from app.main import DAY_KEYS, GenerateRequest, _generate, _sample_payload_dict


def _employee(
    emp_id: str,
    name: str,
    role: str,
    *,
    min_hours: int = 0,
    max_hours: int = 40,
    priority: str = "A",
    student: bool = False,
):
    return {
        "id": emp_id,
        "name": name,
        "role": role,
        "min_hours_per_week": min_hours,
        "max_hours_per_week": max_hours,
        "priority_tier": priority,
        "student": student,
        "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
    }


def test_shoulder_season_blocks_students_on_weekdays():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["shoulder_season"] = True
    payload["open_weekdays"] = ["fri", "sat", "sun"]
    payload["coverage"]["greystones_weekday_staff"] = 2
    payload["coverage"]["greystones_weekend_staff"] = 2
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead_a", "Lead A", "Team Leader"),
        _employee("lead_b", "Lead B", "Team Leader"),
        _employee("clerk_student", "Student Clerk", "Store Clerk", student=True),
        _employee("clerk_regular", "Regular Clerk", "Store Clerk", student=False),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    student_days = {
        a.date
        for a in result.assignments
        if a.employee_id == "clerk_student" and a.location == "Greystones"
    }

    assert "2026-01-09" not in student_days  # Friday
    assert all(day in {"2026-01-10", "2026-01-11"} for day in student_days)


def test_shoulder_season_skips_min_hours_violations():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["shoulder_season"] = True
    payload["open_weekdays"] = ["fri"]
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager", min_hours=0),
        _employee("lead", "Lead", "Team Leader", min_hours=24),
        _employee("clerk", "Clerk", "Store Clerk", min_hours=16),
        _employee("captain", "Captain", "Boat Captain", min_hours=0),
    ]

    result = _generate(GenerateRequest.model_validate(payload))

    assert not any(v.type == "hours_min_violation" for v in result.violations)


def test_manager_off_weekday_requires_two_team_leads_even_if_max_blocked():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["coverage"]["greystones_weekday_staff"] = 2
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead_a", "Lead A", "Team Leader", max_hours=0),
        _employee("lead_b", "Lead B", "Team Leader", max_hours=0),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    payload["unavailability"] = [
        {"employee_id": "manager", "date": "2026-01-05", "reason": "Requested off"},
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    leaders = [
        a
        for a in result.assignments
        if a.date == "2026-01-05" and a.location == "Greystones" and a.role == "Team Leader"
    ]

    assert len(leaders) == 2


def test_team_lead_rotation_uses_prior_week_to_flip_extra_day():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue", "wed", "thu", "fri"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead_a", "Lead A", "Team Leader"),
        _employee("lead_b", "Lead B", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    history_leader_days = {
        (date(2025, 12, 29), "lead_a"): 5,
        (date(2025, 12, 29), "lead_b"): 4,
    }

    result = _generate(
        GenerateRequest.model_validate(payload),
        history_weekly_leader_days=history_leader_days,
    )
    lead_a_days = {
        a.date
        for a in result.assignments
        if a.employee_id == "lead_a" and a.role == "Team Leader" and a.location == "Greystones"
    }
    lead_b_days = {
        a.date
        for a in result.assignments
        if a.employee_id == "lead_b" and a.role == "Team Leader" and a.location == "Greystones"
    }

    assert len(lead_b_days) == len(lead_a_days) + 1


def test_team_lead_rotation_alternates_week_to_week():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 2
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue", "wed", "thu", "fri"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead_a", "Lead A", "Team Leader"),
        _employee("lead_b", "Lead B", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    history_leader_days = {
        (date(2025, 12, 29), "lead_a"): 5,
        (date(2025, 12, 29), "lead_b"): 4,
    }

    result = _generate(
        GenerateRequest.model_validate(payload),
        history_weekly_leader_days=history_leader_days,
    )
    week_one_counts = {"lead_a": 0, "lead_b": 0}
    week_two_counts = {"lead_a": 0, "lead_b": 0}
    for assignment in result.assignments:
        if assignment.role != "Team Leader" or assignment.location != "Greystones":
            continue
        if assignment.date <= "2026-01-11":
            week_one_counts[assignment.employee_id] += 1
        elif "2026-01-12" <= assignment.date <= "2026-01-18":
            week_two_counts[assignment.employee_id] += 1

    assert week_one_counts["lead_b"] == week_one_counts["lead_a"] + 1
    assert week_two_counts["lead_a"] == week_two_counts["lead_b"] + 1


def test_clerk_assignment_prefers_lower_four_week_history_hours():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["coverage"]["greystones_weekday_staff"] = 2
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk_a", "Clerk A", "Store Clerk", priority="B"),
        _employee("clerk_b", "Clerk B", "Store Clerk", priority="B"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    history_hours = {}
    for prior_week in (date(2025, 12, 29), date(2025, 12, 22), date(2025, 12, 15), date(2025, 12, 8)):
        history_hours[(prior_week, "clerk_a")] = 24.0
        history_hours[(prior_week, "clerk_b")] = 0.0

    result = _generate(
        GenerateRequest.model_validate(payload),
        history_weekly_hours=history_hours,
    )
    monday_clerks = [
        a.employee_id
        for a in result.assignments
        if a.date == "2026-01-05" and a.location == "Greystones" and a.role == "Store Clerk"
    ]

    assert monday_clerks == ["clerk_b"]


def test_previous_week_history_prevents_sixth_consecutive_work_day():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["coverage"]["greystones_weekday_staff"] = 2
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk_a", "Clerk A", "Store Clerk", priority="A"),
        _employee("clerk_b", "Clerk B", "Store Clerk", priority="A"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    history_work_days = {
        (date(2025, 12, 29), "clerk_a"): {
            date(2025, 12, 31),
            date(2026, 1, 1),
            date(2026, 1, 2),
            date(2026, 1, 3),
            date(2026, 1, 4),
        }
    }

    result = _generate(
        GenerateRequest.model_validate(payload),
        history_weekly_work_days=history_work_days,
    )
    monday_clerks = [
        a.employee_id
        for a in result.assignments
        if a.date == "2026-01-05" and a.location == "Greystones" and a.role == "Store Clerk"
    ]

    assert monday_clerks == ["clerk_b"]


def test_shoulder_season_weekends_prefer_a_priority_clerks_over_c():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["shoulder_season"] = True
    payload["open_weekdays"] = ["fri", "sat", "sun"]
    payload["coverage"]["greystones_weekday_staff"] = 2
    payload["coverage"]["greystones_weekend_staff"] = 2
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk_a1", "Clerk A1", "Store Clerk", priority="A", student=True),
        _employee("clerk_a2", "Clerk A2", "Store Clerk", priority="A", student=True),
        _employee("clerk_c", "Clerk C", "Store Clerk", priority="C", student=False),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    clerk_c_days = sorted(
        a.date
        for a in result.assignments
        if a.employee_id == "clerk_c" and a.role == "Store Clerk" and a.location == "Greystones"
    )

    assert clerk_c_days == ["2026-01-09"]


def test_shoulder_season_manager_is_scheduled_every_open_day():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["shoulder_season"] = True
    payload["open_weekdays"] = ["fri", "sat", "sun"]
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = True
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead_a", "Lead A", "Team Leader"),
        _employee("lead_b", "Lead B", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    manager_days = {
        a.date
        for a in result.assignments
        if a.employee_id == "manager" and a.role == "Store Manager" and a.location == "Greystones"
    }

    assert manager_days == {"2026-01-09", "2026-01-10", "2026-01-11"}


def test_shoulder_season_and_beach_shop_are_mutually_exclusive():
    payload = _sample_payload_dict()
    payload["schedule_beach_shop"] = True
    payload["shoulder_season"] = True

    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(payload)
