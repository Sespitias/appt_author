-- Tables owned by tebra-appt-authors. Never touches appointment_prod / appointment_update.
CREATE TABLE IF NOT EXISTS `weekly-revenue-integration.Telemind_BD.appointment_authors_staging` (
  ID STRING NOT NULL,
  CreatedByUserID STRING,
  LastModifiedByUserID STRING,
  CreatedDate TIMESTAMP,
  LastModifiedDate TIMESTAMP,
  ConfirmationStatus STRING
);

CREATE TABLE IF NOT EXISTS `weekly-revenue-integration.Telemind_BD.appointment_authors` (
  ID STRING NOT NULL,
  CreatedByUserID STRING,
  LastModifiedByUserID STRING,
  CreatedDate TIMESTAMP,
  LastModifiedDate TIMESTAMP,
  ConfirmationStatus STRING,
  synced_at TIMESTAMP
)
CLUSTER BY ID
OPTIONS (description = 'Creator and last modifier (Tebra user IDs) per appointment ID. Loaded by tebra-appt-authors.');

CREATE TABLE IF NOT EXISTS `weekly-revenue-integration.Telemind_BD.appointment_author_history` (
  ID STRING NOT NULL,
  LastModifiedDate TIMESTAMP NOT NULL,
  LastModifiedByUserID STRING,
  ConfirmationStatus STRING,
  captured_at TIMESTAMP
)
PARTITION BY DATE(LastModifiedDate)
CLUSTER BY ID
OPTIONS (description = 'One row per observed (ID, LastModifiedDate). History starts at job go-live.');
