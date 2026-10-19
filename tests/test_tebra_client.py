from datetime import datetime, timezone
from pathlib import Path

import pytest

from appt_authors.models import AppointmentAuthor
from appt_authors.tebra_client import (
    TebraAuthError,
    TebraTransientError,
    fetch_authors,
    parse_get_appointment,
)

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc


def _fx(name):
    return (FIXTURES / f"get_appointment_{name}.xml").read_bytes()


# --- Task 1: parser -----------------------------------------------------------------------

def test_parse_created_and_updated_by():
    a = parse_get_appointment(_fx("251242"), "251242")
    assert a.id == "251242"
    assert a.created_by_user_id == "984392"
    assert a.last_modified_by_user_id == "832888"
    assert a.created_at_utc == datetime(2026, 10, 12, 16, 6, 18, tzinfo=UTC)
    assert a.updated_at_utc == datetime(2026, 10, 19, 15, 50, 1, tzinfo=UTC)
    assert a.was_created_online is False and a.is_deleted is False
    assert a.fetch_error is None


def test_updated_by_zero_becomes_none():
    a = parse_get_appointment(_fx("252274"), "252274")
    assert a.created_by_user_id == "832888"
    assert a.last_modified_by_user_id is None


def test_parse_not_found_sets_fetch_error():
    a = parse_get_appointment(_fx("not_found"), "1")
    assert a.id == "1"
    assert "does not exist" in a.fetch_error
    assert a.created_by_user_id is None and a.updated_at_utc is None


def test_auth_failure_raises():
    with pytest.raises(TebraAuthError, match="Invalid user name"):
        parse_get_appointment(_fx("bad_auth"), "251242")


def test_throttle_error_is_transient():
    xml = _fx("bad_auth").replace(b"Invalid user name and/or password", b"Request was throttled").replace(
        b"<Authenticated>false</Authenticated><Authorized>false</Authorized>",
        b"<Authenticated>true</Authenticated><Authorized>true</Authorized>",
    ).replace(b"<SecurityResultSuccess>false", b"<SecurityResultSuccess>true")
    with pytest.raises(TebraTransientError):
        parse_get_appointment(xml, "1")


# --- Task 2: throttled fetch with retries -------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _author(i):
    return AppointmentAuthor(str(i), "1", "2", None, None, False, False)


def test_respects_min_interval():
    fc = FakeClock()
    out = list(fetch_authors(["1", "2", "3"], call=_author, deadline=100, min_interval=0.3,
                             sleep=fc.sleep, clock=fc.clock))
    assert [a.id for a in out] == ["1", "2", "3"]
    assert sum(fc.sleeps) >= 2 * 0.3 - 1e-9


def test_stops_at_deadline():
    fc = FakeClock()

    def slow(i):
        fc.t += 1.0
        return _author(i)

    out = list(fetch_authors([str(i) for i in range(10)], call=slow, deadline=3.0, min_interval=0,
                             sleep=fc.sleep, clock=fc.clock))
    assert [a.id for a in out] == ["0", "1", "2"]


def test_retries_transient_then_succeeds():
    fc = FakeClock()
    attempts = []

    def flaky(i):
        attempts.append(i)
        if len(attempts) < 3:
            raise TebraTransientError("503")
        return _author(i)

    out = list(fetch_authors(["7"], call=flaky, deadline=1000, min_interval=0, sleep=fc.sleep, clock=fc.clock))
    assert [a.id for a in out] == ["7"] and len(attempts) == 3


def test_gives_up_after_3_transient_errors_without_losing_previous():
    fc = FakeClock()
    got = []

    def fail_on_b(i):
        if i == "b":
            raise ConnectionError("network down")
        return _author(i)

    with pytest.raises(ConnectionError):
        for a in fetch_authors(["a", "b", "c"], call=fail_on_b, deadline=1000, min_interval=0,
                               sleep=fc.sleep, clock=fc.clock):
            got.append(a.id)
    assert got == ["a"]


def test_auth_error_not_retried():
    calls = []

    def bad(i):
        calls.append(i)
        raise TebraAuthError("Invalid user name and/or password")

    with pytest.raises(TebraAuthError):
        list(fetch_authors(["1", "2"], call=bad, deadline=1000, min_interval=0, sleep=lambda s: None))
    assert calls == ["1"]


def test_client_sets_soap_timeouts(monkeypatch):
    import zeep

    from appt_authors import tebra_client
    from appt_authors.config import Settings

    captured = {}

    class FakeZeep:
        def __init__(self, wsdl, transport=None):
            captured["transport"] = transport

    monkeypatch.setattr(zeep, "Client", FakeZeep)
    s = Settings(tebra_customer_key="k", tebra_user="u", tebra_password="p")
    tebra_client.TebraClient(s)
    t = captured["transport"]
    assert t is not None and t.operation_timeout == 300 and t.load_timeout == 60
