from datetime import date, timedelta

from app.main import _generate, _sample_payload_dict, GenerateRequest


def _payload(**overrides):
    data = _sample_payload_dict()
    data["period"]["start_date"] = (date.today() + timedelta(days=7)).isoformat()
    data.update(overrides)
    return GenerateRequest.model_validate(data)


def test_generate_returns_assignments_for_open_days():
    result = _generate(_payload())
    assert len(result.assignments) > 0


def test_closed_weekdays_produce_no_assignments_on_those_days():
    payload = _payload(open_weekdays=["sat", "sun"])
    result = _generate(payload)
    assert len(result.assignments) > 0
    assert all(date.fromisoformat(a.date).weekday() >= 5 for a in result.assignments)


def test_schedule_aligns_to_selected_week_start_day():
    payload = _sample_payload_dict()
    payload["period"]["start_date"] = "2026-07-07"  # Tuesday
    payload["period"]["weeks"] = 1
    payload["week_start_day"] = "sun"
    payload["week_end_day"] = "sat"
    payload["open_weekdays"] = ["sun"]

    result = _generate(GenerateRequest.model_validate(payload))
    first_assignment_date = min(date.fromisoformat(a.date) for a in result.assignments)
    assert first_assignment_date.isoformat() == "2026-07-12"
