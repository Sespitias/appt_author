"""Entrypoint: python -m appt_authors.main [--max-seconds N] [--limit N]

Each run takes the appointments that appointment_prod has but appointment_authors lacks (or that changed
in prod since we last fetched them), newest first, asks Tebra GetAppointment who created / last modified
each one, and writes them to BigQuery every `batch_size` rows until done or out of time.
"""
import argparse
import json
import logging
import time
import uuid
from dataclasses import replace
from typing import Callable

from .bq import (
    Tables,
    append_history,
    count_pending,
    drop_staging,
    get_pending,
    load_staging,
    merge_current,
)
from .config import Settings
from .models import AppointmentAuthor
from .tebra_client import TebraClient, fetch_authors

# A batch this large with no creator at all means Tebra stopped returning the field.
NULL_AUTHORS_MIN_ROWS = 20


def _check_authors(rows: list[AppointmentAuthor]) -> None:
    fetched = [r for r in rows if r.fetch_error is None]
    if len(fetched) >= NULL_AUTHORS_MIN_ROWS and all(r.created_by_user_id is None for r in fetched):
        raise SystemExit(
            f"{len(fetched)} appointments fetched and none has a CreatedBy: Tebra's GetAppointment response "
            "changed; check scripts/probe_appointment.py"
        )


def _flush(client, tables, rows: list[AppointmentAuthor], result: dict) -> None:
    if not rows:
        return
    _check_authors(rows)
    load_staging(client, tables, rows)
    result["merged"] += merge_current(client, tables)
    result["history_added"] += append_history(client, tables)
    result["processed"] += len(rows)
    result["fetch_errors"] += sum(r.fetch_error is not None for r in rows)
    result["null_created_by"] += sum(r.fetch_error is None and r.created_by_user_id is None for r in rows)
    rows.clear()


def run(*, client=None, tables: Tables | None = None, call: Callable[[str], AppointmentAuthor] | None = None,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        max_seconds: float | None = None, batch_size: int | None = None, min_interval: float | None = None,
        limit: int | None = None) -> dict:
    s = Settings()
    max_seconds = s.max_run_seconds if max_seconds is None else max_seconds
    batch_size = batch_size or s.batch_size
    min_interval = s.min_interval if min_interval is None else min_interval
    if client is None:
        from google.cloud import bigquery

        client = bigquery.Client(project=s.bq_project, location=s.bq_location)
    private_staging = tables is None
    if private_staging:
        tables = Tables.from_settings(s).for_run(uuid.uuid4().hex[:12])
    try:
        return _run(client, tables, call, clock, sleep, max_seconds, batch_size, min_interval, limit, s)
    finally:
        if private_staging:
            drop_staging(client, tables)


def _run(client, tables, call, clock, sleep, max_seconds, batch_size, min_interval, limit, s) -> dict:
    deadline = clock() + max_seconds
    result = {"processed": 0, "merged": 0, "history_added": 0, "fetch_errors": 0, "null_created_by": 0,
              "pending_before": count_pending(client, tables), "pending_after": None, "stopped_by": None}
    # No point reading more IDs than the time budget allows Tebra calls for.
    cap = limit or int(max_seconds / max(min_interval, 0.05)) + batch_size
    pending = get_pending(client, tables, cap)
    if not pending:
        result.update(pending_after=0, stopped_by="no_pending")
        return result

    prod_ts = dict(pending)
    call = call or TebraClient(s).fetch
    buffer: list[AppointmentAuthor] = []
    try:
        for author in fetch_authors(prod_ts, call=call, deadline=deadline, min_interval=min_interval,
                                    sleep=sleep, clock=clock):
            buffer.append(replace(author, prod_last_modified=prod_ts[author.id]))
            if len(buffer) >= batch_size:
                _flush(client, tables, buffer, result)
    finally:
        # Save what was fetched even if Tebra failed midway (a flush error is chained to the original one).
        try:
            _flush(client, tables, buffer, result)
        except Exception:
            logging.exception("could not save partial batch")
            raise
    result["stopped_by"] = "done" if result["processed"] == len(pending) else "deadline"
    result["pending_after"] = count_pending(client, tables)
    return result


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="appt_authors")
    parser.add_argument("--max-seconds", type=float, help="time budget for this run (default MAX_RUN_SECONDS)")
    parser.add_argument("--limit", type=int, help="process at most N appointments")
    args = parser.parse_args(argv)
    print(json.dumps(run(max_seconds=args.max_seconds, limit=args.limit)))


if __name__ == "__main__":
    main()
