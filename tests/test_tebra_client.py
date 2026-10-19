from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from appt_authors.models import AppointmentAuthor
from appt_authors.tebra_client import TebraError, fetch_appointment_authors, parse_appointments

FIXTURE = Path(__file__).parent / "fixtures" / "get_appointments.xml"
UTC = timezone.utc


def _a(id_, modified, by="1"):
    return AppointmentAuthor(id_, by, by, datetime(2026, 1, 1, tzinfo=UTC), modified, "Scheduled")


def test_parse_fixture():
    result = parse_appointments(FIXTURE.read_bytes())
    assert len(result) == 5
    assert all(isinstance(a.id, str) and a.id for a in result)
    by_id = {a.id: a for a in result}
    assert by_id["900001"].created_by_user_id == "1001"
    assert by_id["900001"].last_modified_by_user_id == "1002"
    assert by_id["900001"].created_date == datetime(2026, 10, 17, 9, 15, tzinfo=UTC)
    assert by_id["900001"].last_modified_date == datetime(2026, 10, 18, 14, 40, 10, tzinfo=UTC)
    assert by_id["900003"].last_modified_date == datetime(2026, 10, 18, 16, 5, 33, tzinfo=UTC)
    assert by_id["900005"].created_by_user_id == "1004"
    assert by_id["900005"].confirmation_status == "Check-out"


def test_blank_and_zero_user_ids_become_none():
    by_id = {a.id: a for a in parse_appointments(FIXTURE.read_bytes())}
    assert by_id["900003"].created_by_user_id is None          # ""
    assert by_id["900004"].created_by_user_id is None          # "0"
    assert by_id["900004"].last_modified_by_user_id is None    # absent
    assert by_id["900005"].last_modified_by_user_id is None    # i:nil


def test_parse_respects_timezone():
    result = parse_appointments(FIXTURE.read_bytes(), tz="America/New_York")
    a = {x.id: x for x in result}["900001"]
    assert a.created_date == datetime(2026, 10, 17, 13, 15, tzinfo=UTC)  # EDT = UTC-4


def test_parse_raises_on_tebra_error():
    xml = b"""<Envelope><Body><GetAppointmentsResult><ErrorResponse><IsError>true</IsError>
    <ErrorMessage>Invalid credentials</ErrorMessage></ErrorResponse></GetAppointmentsResult></Body></Envelope>"""
    with pytest.raises(TebraError, match="Invalid credentials"):
        parse_appointments(xml)


def test_chunks_window_by_day():
    calls = []

    def fake(start, end):
        calls.append((start, end))
        return []

    fetch_appointment_authors(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 4, tzinfo=UTC), call=fake)
    assert calls == [
        (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)),
        (datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC)),
        (datetime(2026, 10, 3, tzinfo=UTC), datetime(2026, 10, 4, tzinfo=UTC)),
    ]


def test_halves_window_on_error():
    calls = []

    def fake(start, end):
        calls.append(end - start)
        if end - start > timedelta(hours=12):
            raise RuntimeError("too many records")
        return [_a(f"{start.hour}", end)]

    out = fetch_appointment_authors(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC), call=fake)
    assert calls == [timedelta(days=1), timedelta(hours=12), timedelta(hours=12)]
    assert len(out) == 2


def test_raises_when_window_cannot_shrink():
    def fake(start, end):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        fetch_appointment_authors(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 1, 1, tzinfo=UTC), call=fake)


def test_dedupes_by_id_keeping_latest_modified():
    t1, t2 = datetime(2026, 10, 1, 9, tzinfo=UTC), datetime(2026, 10, 1, 10, tzinfo=UTC)

    def fake(start, end):
        return [_a("1", t2, by="new"), _a("1", t1, by="old"), _a("2", t1)]

    out = {a.id: a for a in fetch_appointment_authors(t1, t1 + timedelta(hours=1), call=fake)}
    assert len(out) == 2
    assert out["1"].last_modified_by_user_id == "new"
