-- Tables owned by tebra-appt-authors. Never touches appointment_prod / appointment_update.
-- Staging tables are per run (appointment_authors_staging_<run id>), created and dropped by the job.
CREATE TABLE IF NOT EXISTS `weekly-revenue-integration.Telemind_BD.appointment_authors` (
  ID STRING NOT NULL,
  CreatedByUserID STRING,
  LastModifiedByUserID STRING,
  CreatedAtUtc TIMESTAMP,
  UpdatedAtUtc TIMESTAMP,
  WasCreatedOnline BOOL,
  IsDeleted BOOL,
  ProdLastModifiedDate TIMESTAMP,
  FetchError STRING,
  synced_at TIMESTAMP
)
CLUSTER BY ID
OPTIONS (description = 'Tebra GetAppointment CreatedBy/UpdatedBy per appointment ID. LastModifiedByUserID is NULL when UpdatedBy=0 (not modified since creation). ProdLastModifiedDate = appointment_prod.LastModifiedDate this row answers for. Loaded by tebra-appt-authors.');

CREATE TABLE IF NOT EXISTS `weekly-revenue-integration.Telemind_BD.appointment_author_history` (
  ID STRING NOT NULL,
  UpdatedAtUtc TIMESTAMP NOT NULL,
  LastModifiedByUserID STRING,
  ProdLastModifiedDate TIMESTAMP,
  captured_at TIMESTAMP
)
PARTITION BY DATE(UpdatedAtUtc)
CLUSTER BY ID
OPTIONS (description = 'One row per observed Tebra modification (ID, UpdatedAtUtc). Only modifications seen by a job run are recorded.');
