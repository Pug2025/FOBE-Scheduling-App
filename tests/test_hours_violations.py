from app.main import DAY_KEYS, GenerateRequest, _generate, _sample_payload_dict


def _employee(emp_id: str, name: str, role: str, *, min_hours: int = 0, max_hours: int = 40):
    return {
        "id": emp_id,
        "name": name,
        "role": role,
        "min_hours_per_week": min_hours,
        "max_hours_per_week": max_hours,
        "priority_tier": "A",
        "availability": {k: ["08:30-17:30"] for k in DAY_KEYS},
    }


def test_weekly_min_hours_breach_is_reported():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["coverage"]["greystones_weekday_staff"] = 2
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader", min_hours=16),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    violations = [v for v in result.violations if v.type == "hours_min_violation"]

    assert any(v.date == "2026-01-05" and "Lead scheduled 8h, minimum is 16h" in v.detail for v in violations)


def test_weekly_max_hours_breach_is_reported():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain", max_hours=0),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    violations = [v for v in result.violations if v.type == "hours_max_violation"]

    assert any(v.date == "2026-01-05" and "Captain scheduled 8h, maximum is 0h" in v.detail for v in violations)


def test_captain_hours_do_not_exceed_max_when_another_captain_is_available():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        {
            **_employee("captain_a", "Captain A", "Boat Captain", max_hours=8),
            "priority_tier": "A",
        },
        {
            **_employee("captain_b", "Captain B", "Boat Captain", max_hours=40),
            "priority_tier": "B",
        },
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    by_employee = result.totals_by_employee

    assert by_employee["captain_a"].week1_hours == 8
    assert by_employee["captain_b"].week1_hours == 8
    assert not any(v for v in result.violations if v.type == "hours_max_violation" and "Captain A" in v.detail)
