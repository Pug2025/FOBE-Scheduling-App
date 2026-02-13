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
