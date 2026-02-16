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


def test_forced_overtime_prefers_lower_priority_employee():
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
        {
            **_employee("captain_a", "Captain A", "Boat Captain", max_hours=0),
            "priority_tier": "A",
        },
        {
            **_employee("captain_c", "Captain C", "Boat Captain", max_hours=0),
            "priority_tier": "C",
        },
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    monday_captains = [
        a.employee_id
        for a in result.assignments
        if a.date == "2026-01-05" and a.location == "Boat" and a.role == "Boat Captain"
    ]

    assert monday_captains == ["captain_c"]


def test_manager_off_leader_days_avoid_preventable_overtime_with_lower_priority_cover():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    payload["coverage"]["greystones_weekday_staff"] = 0
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager", min_hours=0, max_hours=56),
        {
            **_employee("lead_b", "Lead B", "Team Leader", min_hours=0, max_hours=32),
            "priority_tier": "B",
        },
        {
            **_employee("lead_c", "Lead C", "Team Leader", min_hours=0, max_hours=40),
            "priority_tier": "C",
        },
        _employee("captain", "Captain", "Boat Captain", min_hours=0, max_hours=56),
    ]
    payload["unavailability"] = [
        {"employee_id": "manager", "date": "2026-01-11", "reason": "Sunday requested off"},
    ]

    result = _generate(GenerateRequest.model_validate(payload))

    assert result.totals_by_employee["lead_b"].week1_hours <= 32
    assert result.totals_by_employee["lead_c"].week1_hours <= 40
    assert not any(v for v in result.violations if v.type == "hours_max_violation" and "Lead B" in v.detail)


def test_min_hours_makeup_overrides_daily_staff_cap_and_prefers_thu_fri():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue", "wed", "thu", "fri"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager", min_hours=0),
        _employee("lead", "Lead", "Team Leader", min_hours=0),
        _employee("clerk", "Clerk", "Store Clerk", min_hours=16),
        _employee("captain", "Captain", "Boat Captain", min_hours=0),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    clerk_days = sorted(
        a.date
        for a in result.assignments
        if a.employee_id == "clerk" and a.location == "Greystones" and a.role == "Store Clerk"
    )
    thursday_floor = [
        a
        for a in result.assignments
        if a.date == "2026-01-08" and a.location == "Greystones" and a.role in {"Team Leader", "Store Clerk"}
    ]

    assert clerk_days == ["2026-01-08", "2026-01-09"]
    assert len(thursday_floor) > payload["coverage"]["greystones_weekday_staff"]
    assert not any(v for v in result.violations if v.type == "hours_min_violation" and "Clerk" in v.detail)


def test_team_leader_min_hours_makeup_prefers_saturday_then_friday():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["fri", "sat"]
    payload["coverage"]["greystones_weekday_staff"] = 0
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager", min_hours=0),
        {
            **_employee("lead_a", "Lead A", "Team Leader", min_hours=0),
            "priority_tier": "A",
        },
        {
            **_employee("lead_b", "Lead B", "Team Leader", min_hours=8),
            "priority_tier": "B",
        },
        _employee("clerk", "Clerk", "Store Clerk", min_hours=0),
        _employee("captain", "Captain", "Boat Captain", min_hours=0),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    lead_b_days = sorted(
        a.date
        for a in result.assignments
        if a.employee_id == "lead_b" and a.location == "Greystones" and a.role == "Team Leader"
    )

    assert lead_b_days == ["2026-01-10"]
    assert not any(v for v in result.violations if v.type == "hours_min_violation" and "Lead B" in v.detail)


def test_day_off_request_nullifies_min_hours_violation_and_makeup_for_that_week():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["fri", "sat"]
    payload["coverage"]["greystones_weekday_staff"] = 0
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager", min_hours=0),
        {
            **_employee("lead_a", "Lead A", "Team Leader", min_hours=0),
            "priority_tier": "A",
        },
        {
            **_employee("lead_b", "Lead B", "Team Leader", min_hours=8),
            "priority_tier": "B",
        },
        _employee("clerk", "Clerk", "Store Clerk", min_hours=0),
        _employee("captain", "Captain", "Boat Captain", min_hours=0),
    ]
    payload["unavailability"] = [
        {"employee_id": "lead_b", "date": "2026-01-10", "reason": "Requested day off"},
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    lead_b_days = [
        a.date
        for a in result.assignments
        if a.employee_id == "lead_b" and a.location == "Greystones" and a.role == "Team Leader"
    ]

    assert lead_b_days == []
    assert not any(v for v in result.violations if v.type == "hours_min_violation" and "Lead B" in v.detail)
