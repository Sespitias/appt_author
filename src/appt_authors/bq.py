"""BigQuery side: pending appointments, staging load, MERGE into current, append to history."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery

from .config import Settings
from .models import AppointmentAuthor

STAGING_SCHEMA = [
    bigquery.SchemaField("ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("CreatedByUserID", "STRING"),
    bigquery.SchemaField("LastModifiedByUserID", "STRING"),
    bigquery.SchemaField("CreatedAtUtc", "TIMESTAMP"),
    bigquery.SchemaField("UpdatedAtUtc", "TIMESTAMP"),
    bigquery.SchemaField("WasCreatedOnline", "BOOLEAN"),
    bigquery.SchemaField("IsDeleted", "BOOLEAN"),
    bigquery.SchemaField("ProdLastModifiedDate", "TIMESTAMP"),
    bigquery.SchemaField("FetchError", "STRING"),
]

# Appointments in prod with no author row yet, or changed in prod since we fetched them. Newest first,
# so today's changes always go before the historical backlog.
PENDING_WHERE = """
FROM {prod} AS p
LEFT JOIN {current} AS a USING (ID)
WHERE p.ID IS NOT NULL AND (a.ID IS NULL OR a.ProdLastModifiedDate < p.LastModifiedDate)
"""
PENDING_SQL = "SELECT p.ID, p.LastModifiedDate" + PENDING_WHERE + "ORDER BY p.LastModifiedDate DESC LIMIT {limit}"
COUNT_PENDING_SQL = "SELECT COUNT(*) AS n" + PENDING_WHERE

MERGE_CURRENT_SQL = """
MERGE {current} AS T
USING (
  SELECT * FROM {staging}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ID ORDER BY ProdLastModifiedDate DESC) = 1
) AS S
ON T.ID = S.ID
WHEN MATCHED AND S.FetchError IS NOT NULL
  AND (T.ProdLastModifiedDate IS NULL OR S.ProdLastModifiedDate >= T.ProdLastModifiedDate) THEN UPDATE SET
  -- keep whatever authors we already had; just record the error and stop it being pending
  ProdLastModifiedDate = S.ProdLastModifiedDate,
  FetchError = S.FetchError,
  synced_at = CURRENT_TIMESTAMP()
WHEN MATCHED AND S.FetchError IS NULL
  AND (T.ProdLastModifiedDate IS NULL OR S.ProdLastModifiedDate >= T.ProdLastModifiedDate) THEN UPDATE SET
  CreatedByUserID = COALESCE(S.CreatedByUserID, T.CreatedByUserID),
  LastModifiedByUserID = S.LastModifiedByUserID,
  CreatedAtUtc = COALESCE(S.CreatedAtUtc, T.CreatedAtUtc),
  UpdatedAtUtc = S.UpdatedAtUtc,
  WasCreatedOnline = S.WasCreatedOnline,
  IsDeleted = S.IsDeleted,
  ProdLastModifiedDate = S.ProdLastModifiedDate,
  FetchError = NULL,
  synced_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
  (ID, CreatedByUserID, LastModifiedByUserID, CreatedAtUtc, UpdatedAtUtc, WasCreatedOnline, IsDeleted,
   ProdLastModifiedDate, FetchError, synced_at)
  VALUES (S.ID, S.CreatedByUserID, S.LastModifiedByUserID, S.CreatedAtUtc, S.UpdatedAtUtc, S.WasCreatedOnline,
          S.IsDeleted, S.ProdLastModifiedDate, S.FetchError, CURRENT_TIMESTAMP())
"""

APPEND_HISTORY_SQL = """
INSERT INTO {history} (ID, UpdatedAtUtc, LastModifiedByUserID, ProdLastModifiedDate, captured_at)
SELECT S.ID, S.UpdatedAtUtc, S.LastModifiedByUserID, S.ProdLastModifiedDate, CURRENT_TIMESTAMP()
FROM (
  SELECT * FROM {staging}
  WHERE UpdatedAtUtc IS NOT NULL AND FetchError IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ID, UpdatedAtUtc) = 1
) AS S
WHERE NOT EXISTS (
  SELECT 1 FROM {history} AS H
  WHERE H.UpdatedAtUtc >= (SELECT MIN(UpdatedAtUtc) FROM {staging})  -- partition pruning
    AND H.ID = S.ID AND H.UpdatedAtUtc = S.UpdatedAtUtc
)
"""


@dataclass(frozen=True)
class Tables:
    prod: str
    staging: str
    current: str
    history: str

    @classmethod
    def from_settings(cls, s: Settings) -> "Tables":
        return cls(
            prod=s.table("appointment_prod"),
            staging=s.table("appointment_authors_staging"),
            current=s.table("appointment_authors"),
            history=s.table("appointment_author_history"),
        )

    def for_run(self, run_id: str) -> "Tables":
        """Same tables, but a staging table private to one run (concurrent runs can't clobber it)."""
        return replace(self, staging=self.staging[:-1] + f"_{run_id}`")

    def fmt(self, sql: str, **extra) -> str:
        return sql.format(prod=self.prod, staging=self.staging, current=self.current, history=self.history, **extra)


def _run(client: bigquery.Client, sql: str) -> bigquery.QueryJob:
    job = client.query(sql)
    job.result()
    return job


def get_pending(client: bigquery.Client, tables: Tables, limit: int) -> list[tuple[str, datetime]]:
    rows = _run(client, tables.fmt(PENDING_SQL, limit=int(limit))).result()
    return [(r["ID"], r["LastModifiedDate"]) for r in rows]


def count_pending(client: bigquery.Client, tables: Tables) -> int:
    return list(_run(client, tables.fmt(COUNT_PENDING_SQL)).result())[0]["n"]


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_row(a: AppointmentAuthor) -> dict:
    return {
        "ID": a.id,
        "CreatedByUserID": a.created_by_user_id,
        "LastModifiedByUserID": a.last_modified_by_user_id,
        "CreatedAtUtc": _ts(a.created_at_utc),
        "UpdatedAtUtc": _ts(a.updated_at_utc),
        "WasCreatedOnline": a.was_created_online,
        "IsDeleted": a.is_deleted,
        "ProdLastModifiedDate": _ts(a.prod_last_modified),
        "FetchError": a.fetch_error,
    }


def load_staging(client: bigquery.Client, tables: Tables, rows: list[AppointmentAuthor]) -> int:
    config = bigquery.LoadJobConfig(schema=STAGING_SCHEMA, write_disposition="WRITE_TRUNCATE")
    destination = tables.staging.strip("`")
    client.load_table_from_json([_to_row(r) for r in rows], destination, job_config=config).result()
    table = client.get_table(destination)
    table.expires = datetime.now(timezone.utc) + timedelta(days=1)  # safety net if the run dies before dropping it
    client.update_table(table, ["expires"])
    return len(rows)


def drop_staging(client: bigquery.Client, tables: Tables) -> None:
    client.delete_table(tables.staging.strip("`"), not_found_ok=True)


def merge_current(client: bigquery.Client, tables: Tables) -> int:
    return _run(client, tables.fmt(MERGE_CURRENT_SQL)).num_dml_affected_rows or 0


def append_history(client: bigquery.Client, tables: Tables) -> int:
    return _run(client, tables.fmt(APPEND_HISTORY_SQL)).num_dml_affected_rows or 0
