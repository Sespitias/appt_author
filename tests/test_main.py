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
