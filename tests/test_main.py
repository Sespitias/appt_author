from datetime import datetime, timezone

import pytest

from appt_authors import main
from appt_authors.models import AppointmentAuthor

UTC = timezone.utc
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_bq(monkeypatch):
    calls = {"load": [], "merge": 0, "history": 0, "watermark": None}
    monkeypatch.setattr(main, "get_watermark", lambda client, tables: calls["watermark"])
    monkeypatch.setattr(main, "load_staging", lambda client, tables, rows: calls["load"].append(rows) or len(rows))
    monkeypatch.setattr(main, "merge_current", lambda client, tables: calls.__setitem__("merge", calls["merge"] + 1) or 1)
    monkeypatch.setattr(main, "append_history", lambda client, tables: calls.__setitem__("history", calls["history"] + 1) or 1)
    return calls


def _row(i):
    return AppointmentAuthor(str(i), "u", "u", NOW, NOW, "Scheduled")


def test_incremental_uses_watermark_minus_2h(fake_bq):
    fake_bq["watermark"] = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    seen = []
    result = main.run("incremental", client=object(), tables=object(), now=NOW,
                      fetch=lambda s, e: seen.append((s, e)) or [_row(1)])
    assert seen == [(datetime(2026, 10, 19, 8, 0, tzinfo=UTC), NOW)]
    assert result["fetched"] == 1 and fake_bq["merge"] == 1 and fake_bq["history"] == 1


def test_incremental_without_watermark_fails_with_hint(fake_bq):
    with pytest.raises(SystemExit, match="backfill"):
        main.run("incremental", client=object(), tables=object(), now=NOW, fetch=lambda s, e: [])


def test_backfill_splits_by_month(fake_bq):
    seen = []
    main.run("backfill", start=datetime(2026, 8, 15, tzinfo=UTC), end=datetime(2026, 10, 2, tzinfo=UTC),
             client=object(), tables=object(), now=NOW, fetch=lambda s, e: seen.append((s, e)) or [_row(len(seen))])
    assert seen == [
        (datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
        (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
        (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)),
    ]
    assert len(fake_bq["load"]) == 3


def test_skips_bq_writes_when_no_rows(fake_bq):
    fake_bq["watermark"] = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    result = main.run("incremental", client=object(), tables=object(), now=NOW, fetch=lambda s, e: [])
    assert result["fetched"] == 0
    assert fake_bq["load"] == [] and fake_bq["merge"] == 0


class FakeClient:
    def __init__(self):
        self.deleted = []

    def delete_table(self, table, not_found_ok=False):
        self.deleted.append(table)


def test_each_run_uses_its_own_staging_table_and_drops_it(fake_bq, monkeypatch):
    fake_bq["watermark"] = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    staged = []
    monkeypatch.setattr(main, "load_staging", lambda client, tables, rows: staged.append(tables.staging) or len(rows))
    clients = [FakeClient(), FakeClient()]
    for c in clients:
        main.run("incremental", client=c, now=NOW, fetch=lambda s, e: [_row(1)])
    assert staged[0] != staged[1]
    assert all("appointment_authors_staging_" in t for t in staged)
    assert [c.deleted for c in clients] == [[staged[0].strip("`")], [staged[1].strip("`")]]


def test_staging_dropped_even_when_run_fails(fake_bq):
    fake_bq["watermark"] = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    c = FakeClient()

    def boom(s, e):
        raise RuntimeError("tebra down")

    with pytest.raises(RuntimeError):
        main.run("incremental", client=c, now=NOW, fetch=boom)
    assert len(c.deleted) == 1


def test_future_watermark_is_capped_at_now(fake_bq):
    fake_bq["watermark"] = datetime(2099, 1, 1, tzinfo=UTC)
    seen = []
    main.run("incremental", client=FakeClient(), now=NOW, fetch=lambda s, e: seen.append((s, e)) or [])
    assert seen == [(datetime(2026, 10, 19, 10, 0, tzinfo=UTC), NOW)]


def test_fails_when_every_author_is_null(fake_bq):
    fake_bq["watermark"] = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    rows = [AppointmentAuthor(str(i), None, None, NOW, NOW, "Scheduled") for i in range(25)]
    with pytest.raises(SystemExit, match="field names"):
        main.run("incremental", client=FakeClient(), now=NOW, fetch=lambda s, e: rows)
    assert fake_bq["merge"] == 0


def test_reports_null_author_count(fake_bq):
    fake_bq["watermark"] = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    rows = [_row(1), AppointmentAuthor("2", None, "u", NOW, NOW, "Scheduled")]
    result = main.run("incremental", client=FakeClient(), now=NOW, fetch=lambda s, e: rows)
    assert result["null_created_by"] == 1 and result["null_modified_by"] == 0
