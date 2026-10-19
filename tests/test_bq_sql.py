"""Integration tests: run the real SQL against throwaway tables in Telemind_Test_DB.

RUN_BQ_TESTS=1 pytest -m bq -v   (needs GCP credentials with BigQuery write on Telemind_Test_DB)
"""
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from appt_authors.bq import Tables, append_history, get_watermark, load_staging, merge_current
from appt_authors.config import Settings
from appt_authors.models import AppointmentAuthor

pytestmark = [
    pytest.mark.bq,
    pytest.mark.skipif(not os.environ.get("RUN_BQ_TESTS"), reason="set RUN_BQ_TESTS=1 to run BigQuery tests"),
]

UTC = timezone.utc
T1 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
DDL = (Path(__file__).parents[1] / "sql" / "01_tables.sql").read_text()


def _a(id_, modified, created_by="u1", modified_by="u2", status="Scheduled"):
    return AppointmentAuthor(id_, created_by, modified_by, T1, modified, status)


@pytest.fixture
def bq():
    from google.cloud import bigquery

    s = replace(Settings(), bq_dataset="Telemind_Test_DB")
    client = bigquery.Client(project=s.bq_project, location=s.bq_location)
    suffix = "_t" + uuid.uuid4().hex[:8]
    tables = Tables.from_settings(s, suffix)
    ddl = (DDL.replace("Telemind_BD", "Telemind_Test_DB")
              .replace("appointment_authors_staging`", f"appointment_authors_staging{suffix}`")
              .replace("appointment_authors`", f"appointment_authors{suffix}`")
              .replace("appointment_author_history`", f"appointment_author_history{suffix}`"))
    client.query(ddl).result()
    yield client, tables
    for t in (tables.staging, tables.current, tables.history):
        client.delete_table(t.strip("`"), not_found_ok=True)


def _rows(client, table):
    return [dict(r) for r in client.query(f"SELECT * FROM {table} ORDER BY ID, LastModifiedDate").result()]


def _sync(client, tables, rows):
    load_staging(client, tables, rows)
    merge_current(client, tables)
    append_history(client, tables)


def test_merge_inserts_new_ids(bq):
    client, tables = bq
    _sync(client, tables, [_a("1", T1), _a("2", T1)])
    assert [r["ID"] for r in _rows(client, tables.current)] == ["1", "2"]
    assert get_watermark(client, tables) == T1


def test_merge_does_not_regress_to_older_modification(bq):
    client, tables = bq
    _sync(client, tables, [_a("1", T2, modified_by="new")])
    _sync(client, tables, [_a("1", T1, modified_by="old")])
    (row,) = _rows(client, tables.current)
    assert row["LastModifiedByUserID"] == "new"
    assert row["LastModifiedDate"] == T2


def test_merge_keeps_creator_when_source_null(bq):
    client, tables = bq
    _sync(client, tables, [_a("1", T1, created_by="creator")])
    _sync(client, tables, [_a("1", T2, created_by=None)])
    (row,) = _rows(client, tables.current)
    assert row["CreatedByUserID"] == "creator"
    assert row["LastModifiedDate"] == T2


def test_history_is_idempotent(bq):
    client, tables = bq
    batch = [_a("1", T1), _a("2", T1)]
    _sync(client, tables, batch)
    _sync(client, tables, batch)
    assert len(_rows(client, tables.history)) == 2


def test_history_records_each_distinct_modification(bq):
    client, tables = bq
    _sync(client, tables, [_a("1", T1, modified_by="a")])
    _sync(client, tables, [_a("1", T2, modified_by="b")])
    assert [r["LastModifiedByUserID"] for r in _rows(client, tables.history)] == ["a", "b"]
