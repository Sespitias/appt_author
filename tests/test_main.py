from datetime import datetime, timezone

import pytest

from appt_authors import main
from appt_authors.models import AppointmentAuthor
from appt_authors.tebra_client import TebraAuthError

UTC = timezone.utc
PROD_TS = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self):
        self.deleted = []

    def delete_table(self, table, not_found_ok=False):
        self.deleted.append(table)


class FakeClock:
    def __init__(self, step=0.0):
        self.t, self.step = 0.0, step

    def clock(self):
        self.t += self.step
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def fake_bq(monkeypatch):
    state = {"pending": [], "loads": [], "merges": 0, "history": 0, "staging": []}

    def get_pending(client, tables, limit):
        return state["pending"][:limit]

    def load_staging(client, tables, rows):
        state["loads"].append(list(rows))
        state["staging"].append(tables.staging)
        done = {r.id for r in rows}
        state["pending"] = [p for p in state["pending"] if p[0] not in done]
        return len(rows)

    monkeypatch.setattr(main, "get_pending", get_pending)
    monkeypatch.setattr(main, "count_pending", lambda client, tables: len(state["pending"]))
    monkeypatch.setattr(main, "load_staging", load_staging)
    monkeypatch.setattr(main, "merge_current", lambda c, t: state.__setitem__("merges", state["merges"] + 1) or 1)
    monkeypatch.setattr(main, "append_history", lambda c, t: state.__setitem__("history", state["history"] + 1) or 1)
    return state


def _ok(i):
    return AppointmentAuthor(i, "100", "200", None, None, False, False)


def _run(client=None, **kw):
    fc = kw.pop("fc", FakeClock())
    return main.run(client=client or FakeClient(), call=kw.pop("call", _ok), clock=fc.clock, sleep=fc.sleep,
                    max_seconds=kw.pop("max_seconds", 600), batch_size=kw.pop("batch_size", 500),
                    min_interval=0, **kw)


def test_flushes_every_batch_size(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(1200)]
    result = _run()
    assert [len(b) for b in fake_bq["loads"]] == [500, 500, 200]
    assert result["processed"] == 1200 and result["stopped_by"] == "done"
    assert result["pending_before"] == 1200 and result["pending_after"] == 0


def test_rows_carry_prod_last_modified(fake_bq):
    fake_bq["pending"] = [("7", PROD_TS)]
    _run()
    assert fake_bq["loads"][0][0].prod_last_modified == PROD_TS


def test_partial_progress_saved_on_tebra_failure(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(20)]

    def flaky(i):  # Tebra goes down from the 7th appointment on
        if int(i) >= 6:
            raise ConnectionError("network down")
        return _ok(i)

    with pytest.raises(ConnectionError):
        _run(call=flaky, batch_size=500)
    assert [r.id for r in fake_bq["loads"][0]] == [str(i) for i in range(6)]


def test_auth_error_fails_fast_without_writes(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(10)]

    def bad(i):
        raise TebraAuthError("Invalid user name and/or password")

    with pytest.raises(TebraAuthError):
        _run(call=bad)
    assert fake_bq["loads"] == []


def test_stops_by_deadline_and_reports_it(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(100)]
    result = _run(fc=FakeClock(step=1.0), max_seconds=20, flush_reserve=0)
    assert 0 < result["processed"] < 100
    assert result["stopped_by"] == "deadline"
    assert result["pending_after"] == 100 - result["processed"]


def test_no_pending_does_nothing(fake_bq):
    result = _run()
    assert result["processed"] == 0 and result["stopped_by"] == "no_pending"
    assert fake_bq["loads"] == []


def test_null_author_guard(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(25)]
    with pytest.raises(SystemExit, match="CreatedBy"):
        _run(call=lambda i: AppointmentAuthor(i, None, None, None, None, None, None))
    assert fake_bq["merges"] == 0


def test_fetch_errors_do_not_trip_null_guard(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(25)]
    result = _run(call=lambda i: AppointmentAuthor(i, None, None, None, None, None, None, fetch_error="404"))
    assert result["fetch_errors"] == 25


def test_each_run_uses_its_own_staging_table_and_drops_it(fake_bq):
    clients = [FakeClient(), FakeClient()]
    for c in clients:
        fake_bq["pending"] = [("1", PROD_TS)]
        _run(client=c)
    a, b = fake_bq["staging"]
    assert a != b and "appointment_authors_staging_" in a
    assert [c.deleted for c in clients] == [[a.strip("`")], [b.strip("`")]]


def test_staging_dropped_even_when_run_fails(fake_bq):
    fake_bq["pending"] = [("1", PROD_TS)]
    c = FakeClient()
    with pytest.raises(TebraAuthError):
        _run(client=c, call=lambda i: (_ for _ in ()).throw(TebraAuthError("bad")))
    assert len(c.deleted) == 1


def test_fetching_leaves_time_for_final_flush(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(1000)]
    result = _run(fc=FakeClock(step=1.0), max_seconds=100, flush_reserve=60)
    assert result["processed"] <= 40


def test_stopped_by_done_only_when_nothing_pending(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(10)]
    result = _run(limit=4)
    assert result["processed"] == 4 and result["stopped_by"] == "limit" and result["pending_after"] == 6


def test_recurring_occurrence_is_fetched_by_series_id(fake_bq):
    fake_bq["pending"] = [("163379KKF", PROD_TS)]
    asked = []
    _run(call=lambda i: asked.append(i) or _ok(i))
    assert asked == ["163379"]
    assert fake_bq["loads"][0][0].id == "163379KKF"


def test_unusable_id_recorded_as_fetch_error_without_calling_tebra(fake_bq):
    fake_bq["pending"] = [("KKF", PROD_TS)]
    asked = []
    result = _run(call=lambda i: asked.append(i) or _ok(i))
    assert asked == [] and result["fetch_errors"] == 1


def test_null_author_guard_ignores_online_bookings(fake_bq):
    fake_bq["pending"] = [(str(i), PROD_TS) for i in range(25)]
    result = _run(call=lambda i: AppointmentAuthor(i, None, None, None, None, True, False))
    assert result["processed"] == 25


def test_dockerfile_cmd_is_accepted_by_cli():
    import json
    import re
    from pathlib import Path

    docker = (Path(__file__).parents[1] / "Dockerfile").read_text()
    m = re.search(r"^CMD (\[.*\])", docker, re.M)
    args = json.loads(m.group(1)) if m else []
    main.build_parser().parse_args(args)  # SystemExit (exit 2) would fail the test
