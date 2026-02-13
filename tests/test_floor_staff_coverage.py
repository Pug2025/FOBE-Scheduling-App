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


def test_weekday_coverage_counts_only_leads_and_clerks():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["coverage"]["greystones_weekday_staff"] = 3
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk1", "Clerk 1", "Store Clerk"),
        _employee("clerk2", "Clerk 2", "Store Clerk"),
        _employee("clerk3", "Clerk 3", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    day_assignments = [a for a in result.assignments if a.date == "2026-01-05" and a.location == "Greystones"]

    floor_staff = [a for a in day_assignments if a.role in {"Team Leader", "Store Clerk"}]
    assert len(floor_staff) == 3
    assert any(a.role == "Store Manager" for a in day_assignments)


def test_weekend_coverage_counts_only_leads_and_clerks():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["sat"]
    payload["coverage"]["greystones_weekend_staff"] = 4
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead1", "Lead 1", "Team Leader"),
        _employee("lead2", "Lead 2", "Team Leader"),
        _employee("clerk1", "Clerk 1", "Store Clerk"),
        _employee("clerk2", "Clerk 2", "Store Clerk"),
        _employee("clerk3", "Clerk 3", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    day_assignments = [a for a in result.assignments if a.date == "2026-01-10" and a.location == "Greystones"]

    floor_staff = [a for a in day_assignments if a.role in {"Team Leader", "Store Clerk"}]
    assert len(floor_staff) == 4
    assert any(a.role == "Store Manager" for a in day_assignments)


def test_weekend_manager_day_off_with_one_lead_off_uses_extra_clerk():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["sat"]
    payload["coverage"]["greystones_weekend_staff"] = 2
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead1", "Lead 1", "Team Leader"),
        _employee("lead2", "Lead 2", "Team Leader"),
        _employee("clerk1", "Clerk 1", "Store Clerk"),
        _employee("clerk2", "Clerk 2", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    payload["unavailability"] = [
        {"employee_id": "manager", "date": "2026-01-10", "reason": "Weekend off"},
        {"employee_id": "lead2", "date": "2026-01-10", "reason": "Weekend off"},
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    day_assignments = [a for a in result.assignments if a.date == "2026-01-10" and a.location == "Greystones"]
    leaders = [a for a in day_assignments if a.role == "Team Leader"]
    clerks = [a for a in day_assignments if a.role == "Store Clerk"]

    assert len(leaders) == 1
    assert len(clerks) >= 1


def test_weekend_manager_off_still_gets_two_leads_even_if_max_hours_would_block():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["sat"]
    payload["coverage"]["greystones_weekend_staff"] = 2
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead1", "Lead 1", "Team Leader"),
        _employee("lead2", "Lead 2", "Team Leader"),
        _employee("clerk1", "Clerk 1", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    payload["unavailability"] = [
        {"employee_id": "manager", "date": "2026-01-10", "reason": "Weekend off"},
    ]
    for emp in payload["employees"]:
        if emp["role"] == "Team Leader":
            emp["max_hours_per_week"] = 0

    result = _generate(GenerateRequest.model_validate(payload))
    day_assignments = [a for a in result.assignments if a.date == "2026-01-10" and a.location == "Greystones"]
    leaders = [a for a in day_assignments if a.role == "Team Leader"]

    assert len(leaders) == 2
