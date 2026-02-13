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


def test_reroll_token_can_change_selected_assignments():
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
        _employee("captain_a", "Captain A", "Boat Captain"),
        _employee("captain_b", "Captain B", "Boat Captain"),
    ]

    chosen = set()
    for token in range(6):
        payload["reroll_token"] = token
        result = _generate(GenerateRequest.model_validate(payload))
        monday_captain = next(a.employee_id for a in result.assignments if a.date == "2026-01-05" and a.location == "Boat")
        chosen.add(monday_captain)

    assert len(chosen) > 1
