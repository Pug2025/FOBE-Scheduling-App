from app.main import GenerateRequest, _generate, _sample_payload_dict


def test_manager_default_off_pair_prefers_weekdays_over_weekend():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-07-05"  # Sunday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "sun"
    payload["week_end_day"] = "sat"
    payload["open_weekdays"] = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    payload["unavailability"] = []
    payload["leadership_rules"]["manager_two_consecutive_days_off_per_week"] = True

    result = _generate(GenerateRequest.model_validate(payload))
    manager_days = {a.date for a in result.assignments if a.role == "Store Manager"}

    assert "2026-07-05" in manager_days
    assert "2026-07-11" in manager_days
