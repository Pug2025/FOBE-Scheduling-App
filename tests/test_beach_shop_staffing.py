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


def test_beach_shop_gets_two_staff_on_open_weekday():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-07-06"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["schedule_beach_shop"] = True
    payload["coverage"]["greystones_weekday_staff"] = 2
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    beach_assignments = [a for a in result.assignments if a.location == "Beach Shop" and a.date == "2026-07-06"]

    assert len(beach_assignments) == 2
    assert {a.role for a in beach_assignments} == {"Store Clerk", "Team Leader"}
    assert not any(v for v in result.violations if v.type == "beach_shop_gap" and v.date == "2026-07-06")


def test_beach_shop_weekend_keeps_two_staff_even_if_max_hours_reached():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-07-05"  # Sunday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "sun"
    payload["week_end_day"] = "sat"
    payload["open_weekdays"] = ["sat", "sun"]
    payload["schedule_beach_shop"] = True
    payload["coverage"]["greystones_weekend_staff"] = 2
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    # Greystones shifts are 8h/day net, so a max of 8 would otherwise block Beach Shop
    # if Beach Shop enforced max-hours constraints.
    for emp in payload["employees"]:
        if emp["role"] in {"Team Leader", "Store Clerk"}:
            emp["max_hours_per_week"] = 8

    result = _generate(GenerateRequest.model_validate(payload))
    weekend_beach = [a for a in result.assignments if a.location == "Beach Shop" and a.date in {"2026-07-05", "2026-07-11"}]

    assert len([a for a in weekend_beach if a.date == "2026-07-05"]) == 2
    assert len([a for a in weekend_beach if a.date == "2026-07-11"]) == 2
    assert not any(v for v in result.violations if v.type == "beach_shop_gap" and v.date in {"2026-07-05", "2026-07-11"})


def test_beach_shop_gets_weekend_staff_outside_summer():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-02-15"  # Sunday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "sun"
    payload["week_end_day"] = "sat"
    payload["open_weekdays"] = ["sat", "sun"]
    payload["schedule_beach_shop"] = True
    payload["coverage"]["greystones_weekend_staff"] = 2
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    beach_assignments = [a for a in result.assignments if a.location == "Beach Shop"]

    assert len([a for a in beach_assignments if a.date == "2026-02-15"]) == 2
    assert len([a for a in beach_assignments if a.date == "2026-02-21"]) == 2
    assert not any(v for v in result.violations if v.type == "beach_shop_gap" and v.date in {"2026-02-15", "2026-02-21"})


def test_store_plus_beach_same_day_does_not_double_count_hours_or_days():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-07-05"  # Sunday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "sun"
    payload["week_end_day"] = "sat"
    payload["open_weekdays"] = ["sun"]
    payload["schedule_beach_shop"] = True
    payload["coverage"]["greystones_weekend_staff"] = 2
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    totals = result.totals_by_employee

    assert totals["lead"].week1_hours == 8
    assert totals["lead"].week1_days == 1
    assert totals["clerk"].week1_hours == 8
    assert totals["clerk"].week1_days == 1
