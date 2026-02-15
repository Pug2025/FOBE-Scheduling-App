from app.main import DAY_KEYS, GenerateRequest, _generate, _sample_payload_dict


def _employee(
    emp_id: str,
    name: str,
    role: str,
    *,
    min_hours: int = 0,
    max_hours: int = 40,
    availability: dict[str, list[str]] | None = None,
):
    return {
        "id": emp_id,
        "name": name,
        "role": role,
        "min_hours_per_week": min_hours,
        "max_hours_per_week": max_hours,
        "priority_tier": "A",
        "student": False,
        "availability": availability or {k: ["08:30-17:30"] for k in DAY_KEYS},
    }


def test_ad_hoc_booking_is_added_as_bolt_on_shift():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    partial_day_availability = {k: [] for k in DAY_KEYS}
    partial_day_availability["mon"] = ["12:00-16:00"]
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager"),
        _employee("lead", "Lead", "Team Leader"),
        _employee("clerk_adhoc", "Clerk Ad Hoc", "Store Clerk", availability=partial_day_availability),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    payload["ad_hoc_bookings"] = [
        {"employee_id": "clerk_adhoc", "date": "2026-01-05", "start": "12:00", "end": "16:00", "location": "Greystones", "note": "Lunch rush"},
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    ad_hoc_match = [
        a
        for a in result.assignments
        if a.employee_id == "clerk_adhoc" and a.date == "2026-01-05" and a.location == "Greystones" and a.start == "12:00" and a.end == "16:00"
    ]
    baseline_floor = [
        a
        for a in result.assignments
        if a.date == "2026-01-05" and a.location == "Greystones" and a.role in {"Team Leader", "Store Clerk"} and a.start == "08:30" and a.end == "17:30"
    ]

    assert len(ad_hoc_match) == 1
    assert ad_hoc_match[0].source == "ad_hoc"
    assert len(baseline_floor) == 1
    assert result.totals_by_employee["clerk_adhoc"].week1_hours == 4
    assert not any(v for v in result.violations if v.type == "ad_hoc_conflict")


def test_ad_hoc_booking_respects_max_hours_and_is_skipped_on_conflict():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-01-05"  # Monday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "mon"
    payload["week_end_day"] = "sun"
    payload["open_weekdays"] = ["mon", "tue"]
    payload["coverage"]["greystones_weekday_staff"] = 1
    payload["coverage"]["greystones_weekend_staff"] = 0
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = False
    payload["employees"] = [
        _employee("manager", "Manager", "Store Manager", max_hours=8),
        _employee("lead_a", "Lead A", "Team Leader"),
        _employee("lead_b", "Lead B", "Team Leader"),
        _employee("clerk", "Clerk", "Store Clerk"),
        _employee("captain", "Captain", "Boat Captain"),
    ]
    payload["ad_hoc_bookings"] = [
        {"employee_id": "manager", "date": "2026-01-06", "start": "12:00", "end": "16:00", "location": "Greystones", "note": "Extra support"},
    ]

    result = _generate(GenerateRequest.model_validate(payload))
    ad_hoc_match = [
        a
        for a in result.assignments
        if a.employee_id == "manager" and a.date == "2026-01-06" and a.location == "Greystones" and a.start == "12:00" and a.end == "16:00"
    ]
    conflicts = [v for v in result.violations if v.type == "ad_hoc_conflict" and v.date == "2026-01-06"]

    assert ad_hoc_match == []
    assert any("would exceed weekly max hours" in v.detail for v in conflicts)
