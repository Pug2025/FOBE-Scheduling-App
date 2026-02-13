from app.main import DAY_KEYS, GenerateRequest, _generate, _sample_payload_dict


def _employee(emp_id: str, name: str, role: str):
    return {
        "id": emp_id,
        "name": name,
        "role": role,
        "min_hours_per_week": 0,
        "max_hours_per_week": 40,
        "priority_tier": "A",
        "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
    }


def _max_consecutive_days_off(open_days: list[str], worked_days: set[str]) -> int:
    streak = 0
    max_streak = 0
    for day in open_days:
        if day in worked_days:
            streak = 0
            continue
        streak += 1
        max_streak = max(max_streak, streak)
    return max_streak


def test_team_leader_assignment_prefers_consecutive_on_blocks():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue", "wed", "thu"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["coverage"]["greystones_weekend_staff"] = 1
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead_a", "Lead A", "Team Leader"),
        _employee("lead_b", "Lead B", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    open_days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    ordered_leads = [
        next(
            a.employee_id
            for a in result.assignments
            if a.date == day and a.location == "Greystones" and a.role == "Team Leader"
        )
        for day in open_days
    ]

    assert any(ordered_leads[i] == ordered_leads[i + 1] for i in range(len(ordered_leads) - 1))


def test_team_leaders_prioritize_not_exceeding_two_days_off_when_avoidable():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue", "wed", "thu", "fri"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["coverage"]["greystones_weekend_staff"] = 1
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead_a", "Lead A", "Team Leader"),
        _employee("lead_b", "Lead B", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    open_days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    lead_work = {
        lead_id: {
            a.date
            for a in result.assignments
            if a.employee_id == lead_id and a.role == "Team Leader" and a.location == "Greystones"
        }
        for lead_id in ("lead_a", "lead_b")
    }

    assert _max_consecutive_days_off(open_days, lead_work["lead_a"]) <= 2
    assert _max_consecutive_days_off(open_days, lead_work["lead_b"]) <= 2
