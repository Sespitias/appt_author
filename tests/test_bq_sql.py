"""Integration tests: run the real SQL against throwaway tables in Telemind_Test_DB.

RUN_BQ_TESTS=1 pytest -m bq -v   (needs GCP credentials with BigQuery write on Telemind_Test_DB)
"""
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from appt_authors.bq import Tables, append_history, count_pending, get_pending, load_staging, merge_current
from appt_authors.config import Settings
from appt_authors.models import AppointmentAuthor

pytestmark = [
    pytest.mark.bq,
    pytest.mark.skipif(not os.environ.get("RUN_BQ_TESTS"), reason="set RUN_BQ_TESTS=1 to run BigQuery tests"),
]

UTC = timezone.utc
DDL = (Path(__file__).parents[1] / "sql" / "01_tables.sql").read_text()


def T(h):
    return datetime(2026, 10, 19, h, tzinfo=UTC)


def _a(id_, prod_h, updated_h=None, created_by="c", modified_by="m", error=None):
    return AppointmentAuthor(id_, created_by, modified_by, T(8), T(updated_h) if updated_h else None,
                             False, False, prod_last_modified=T(prod_h), fetch_error=error)


@pytest.fixture
def bq():
    from google.cloud import bigquery

    s = replace(Settings(), bq_dataset="Telemind_Test_DB")
    client = bigquery.Client(project=s.bq_project, location=s.bq_location)
    sfx = "_t" + uuid.uuid4().hex[:8]
    base = Tables.from_settings(s)
    tables = Tables(prod=base.prod[:-1] + sfx + "`", staging=base.staging[:-1] + sfx + "`",
                    current=base.current[:-1] + sfx + "`", history=base.history[:-1] + sfx + "`")
    client.query(DDL.replace("Telemind_BD", "Telemind_Test_DB")
                    .replace("appointment_authors`", f"appointment_authors{sfx}`")
                    .replace("appointment_author_history`", f"appointment_author_history{sfx}`")).result()
    client.query(f"""CREATE TABLE {tables.prod} AS SELECT * FROM UNNEST([
        STRUCT('A' AS ID, TIMESTAMP '{T(2).isoformat()}' AS LastModifiedDate),
        ('B', TIMESTAMP '{T(1).isoformat()}'), ('C', TIMESTAMP '{T(3).isoformat()}'),
        ('D', TIMESTAMP '{T(1).isoformat()}')])""").result()
    yield client, tables
    for t in (tables.prod, tables.staging, tables.current, tables.history):
        client.delete_table(t.strip("`"), not_found_ok=True)


def _sync(client, tables, rows):
    load_staging(client, tables, rows)
    merge_current(client, tables)
    append_history(client, tables)


def _one(client, sql):
    return list(client.query(sql).result())[0]


def test_pending_is_newest_first(bq):
    client, tables = bq
    assert [i for i, _ in get_pending(client, tables, 10)][:2] == ["C", "A"]
    assert count_pending(client, tables) == 4


def test_processed_and_errored_rows_stop_being_pending(bq):
    client, tables = bq
    _sync(client, tables, [_a("A", 2, 10), _a("B", 1, error="does not exist"), _a("C", 3, 11)])
    assert [i for i, _ in get_pending(client, tables, 10)] == ["D"]
    assert _one(client, f"SELECT COUNT(*) n FROM {tables.history}")["n"] == 2


def test_history_is_idempotent(bq):
    client, tables = bq
    batch = [_a("A", 2, 10), _a("C", 3, 11)]
    _sync(client, tables, batch)
    _sync(client, tables, batch)
    assert _one(client, f"SELECT COUNT(*) n FROM {tables.history}")["n"] == 2


def test_merge_does_not_regress(bq):
    client, tables = bq
    _sync(client, tables, [_a("A", 2, 10, modified_by="new")])
    _sync(client, tables, [_a("A", 1, 9, modified_by="old")])
    assert _one(client, f"SELECT LastModifiedByUserID m FROM {tables.current} WHERE ID='A'")["m"] == "new"


def test_error_keeps_previous_authors(bq):
    client, tables = bq
    _sync(client, tables, [_a("A", 2, 10, modified_by="m1")])
    _sync(client, tables, [_a("A", 3, created_by=None, modified_by=None, error="does not exist")])
    row = _one(client, f"SELECT LastModifiedByUserID m, FetchError e FROM {tables.current} WHERE ID='A'")
    assert row["m"] == "m1" and row["e"] == "does not exist"
