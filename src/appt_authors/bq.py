"""BigQuery side: watermark, staging load, MERGE into current, append to history."""
from dataclasses import dataclass
from datetime import datetime

from google.cloud import bigquery

from .config import Settings
from .models import AppointmentAuthor

STAGING_SCHEMA = [
    bigquery.SchemaField("ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("CreatedByUserID", "STRING"),
    bigquery.SchemaField("LastModifiedByUserID", "STRING"),
    bigquery.SchemaField("CreatedDate", "TIMESTAMP"),
    bigquery.SchemaField("LastModifiedDate", "TIMESTAMP"),
    bigquery.SchemaField("ConfirmationStatus", "STRING"),
]

MERGE_CURRENT_SQL = """
MERGE {current} AS T
USING (
  SELECT * EXCEPT(rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY ID ORDER BY LastModifiedDate DESC) AS rn
    FROM {staging}
  ) WHERE rn = 1
) AS S
ON T.ID = S.ID
WHEN MATCHED AND (T.LastModifiedDate IS NULL OR S.LastModifiedDate >= T.LastModifiedDate) THEN UPDATE SET
  CreatedByUserID = COALESCE(S.CreatedByUserID, T.CreatedByUserID),
  LastModifiedByUserID = S.LastModifiedByUserID,
  CreatedDate = COALESCE(S.CreatedDate, T.CreatedDate),
  LastModifiedDate = S.LastModifiedDate,
  ConfirmationStatus = S.ConfirmationStatus,
  synced_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
  (ID, CreatedByUserID, LastModifiedByUserID, CreatedDate, LastModifiedDate, ConfirmationStatus, synced_at)
  VALUES (S.ID, S.CreatedByUserID, S.LastModifiedByUserID, S.CreatedDate, S.LastModifiedDate, S.ConfirmationStatus, CURRENT_TIMESTAMP())
"""

APPEND_HISTORY_SQL = """
INSERT INTO {history} (ID, LastModifiedDate, LastModifiedByUserID, ConfirmationStatus, captured_at)
SELECT S.ID, S.LastModifiedDate, S.LastModifiedByUserID, S.ConfirmationStatus, CURRENT_TIMESTAMP()
FROM (
  SELECT * FROM {staging}
  WHERE LastModifiedDate IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ID, LastModifiedDate) = 1
) AS S
WHERE NOT EXISTS (
  SELECT 1 FROM {history} AS H
  WHERE H.ID = S.ID AND H.LastModifiedDate = S.LastModifiedDate
)
"""

WATERMARK_SQL = "SELECT MAX(LastModifiedDate) AS wm FROM {current}"


@dataclass(frozen=True)
class Tables:
    staging: str
    current: str
    history: str

    @classmethod
    def from_settings(cls, s: Settings, suffix: str = "") -> "Tables":
        return cls(
            staging=s.table(f"appointment_authors_staging{suffix}"),
            current=s.table(f"appointment_authors{suffix}"),
            history=s.table(f"appointment_author_history{suffix}"),
        )

    def fmt(self, sql: str) -> str:
        return sql.format(staging=self.staging, current=self.current, history=self.history)


def _run(client: bigquery.Client, sql: str) -> bigquery.QueryJob:
    job = client.query(sql)
    job.result()
    return job


def get_watermark(client: bigquery.Client, tables: Tables) -> datetime | None:
    rows = list(_run(client, tables.fmt(WATERMARK_SQL)).result())
    return rows[0]["wm"] if rows else None


def _to_row(a: AppointmentAuthor) -> dict:
    return {
        "ID": a.id,
        "CreatedByUserID": a.created_by_user_id,
        "LastModifiedByUserID": a.last_modified_by_user_id,
        "CreatedDate": a.created_date.isoformat() if a.created_date else None,
        "LastModifiedDate": a.last_modified_date.isoformat() if a.last_modified_date else None,
        "ConfirmationStatus": a.confirmation_status,
    }


def load_staging(client: bigquery.Client, tables: Tables, rows: list[AppointmentAuthor]) -> int:
    config = bigquery.LoadJobConfig(schema=STAGING_SCHEMA, write_disposition="WRITE_TRUNCATE")
    destination = tables.staging.strip("`")
    client.load_table_from_json([_to_row(r) for r in rows], destination, job_config=config).result()
    return len(rows)


def merge_current(client: bigquery.Client, tables: Tables) -> int:
    return _run(client, tables.fmt(MERGE_CURRENT_SQL)).num_dml_affected_rows or 0


def append_history(client: bigquery.Client, tables: Tables) -> int:
    return _run(client, tables.fmt(APPEND_HISTORY_SQL)).num_dml_affected_rows or 0
