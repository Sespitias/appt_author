"""Entrypoint: python -m appt_authors.main incremental | backfill --start YYYY-MM-DD [--end YYYY-MM-DD]"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from .bq import Tables, append_history, get_watermark, load_staging, merge_current
from .config import Settings
from .models import AppointmentAuthor
from .tebra_client import TebraClient, fetch_appointment_authors

OVERLAP = timedelta(hours=2)

Fetch = Callable[[datetime, datetime], list[AppointmentAuthor]]


def _month_windows(start: datetime, end: datetime):
    cursor = start
    while cursor < end:
        first_next = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = min(first_next, end)
        yield cursor, nxt
        cursor = nxt


def _sync(client, tables, rows: list[AppointmentAuthor]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    load_staging(client, tables, rows)
    return merge_current(client, tables), append_history(client, tables)


def run(mode: str, start: datetime | None = None, end: datetime | None = None, *,
        client=None, tables: Tables | None = None, fetch: Fetch | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if fetch is None:
        tebra = TebraClient()  # one WSDL load per run
        fetch = lambda s, e: fetch_appointment_authors(s, e, call=tebra.get_modified)  # noqa: E731
    if client is None:
        from google.cloud import bigquery

        s = Settings()
        client = bigquery.Client(project=s.bq_project, location=s.bq_location)
        tables = tables or Tables.from_settings(s)

    if mode == "incremental":
        watermark = get_watermark(client, tables)
        if watermark is None:
            raise SystemExit("appointment_authors is empty: run `backfill --start 2018-09-01` first")
        windows = [(watermark - OVERLAP, now)]
    elif mode == "backfill":
        if start is None:
            raise SystemExit("backfill needs --start")
        windows = list(_month_windows(start, end or now))
    else:
        raise SystemExit(f"unknown mode {mode!r}")

    result = {"mode": mode, "window_start": windows[0][0].isoformat(), "window_end": windows[-1][1].isoformat(),
              "fetched": 0, "merged": 0, "history_added": 0}
    for w_start, w_end in windows:
        rows = fetch(w_start, w_end)
        merged, added = _sync(client, tables, rows)
        result["fetched"] += len(rows)
        result["merged"] += merged
        result["history_added"] += added
        if mode == "backfill":
            logging.info("backfill window %s→%s: %d rows", w_start.date(), w_end.date(), len(rows))
    return result


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="appt_authors")
    parser.add_argument("mode", choices=["incremental", "backfill"])
    parser.add_argument("--start", type=_date)
    parser.add_argument("--end", type=_date)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.mode, args.start, args.end)))


if __name__ == "__main__":
    main()
