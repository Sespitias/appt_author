-- appointment_prod + who created / last modified each appointment (Tebra GetAppointment).
-- Tebra reports UpdatedBy=0 when an appointment was not modified after creation, so the effective
-- last modifier falls back to the creator. tebra_users is unique per user_id, so no fan-out.
-- Note: appointment_prod dates are Pacific local time labelled UTC; the *Utc columns are real UTC.
CREATE OR REPLACE VIEW `weekly-revenue-integration.Telemind_BD.appointment_prod_authors` AS
SELECT
  p.*,
  a.CreatedByUserID,
  cu.full_name AS CreatedByName,
  cu.email AS CreatedByEmail,
  COALESCE(a.LastModifiedByUserID, a.CreatedByUserID) AS LastModifiedByUserID,
  mu.full_name AS LastModifiedByName,
  mu.email AS LastModifiedByEmail,
  a.WasCreatedOnline,
  a.CreatedAtUtc,
  a.UpdatedAtUtc,
  a.ProdLastModifiedDate < p.LastModifiedDate AS AuthorsPending,
  a.FetchError AS AuthorsFetchError,
  a.synced_at AS AuthorsSyncedAt
FROM `weekly-revenue-integration.Telemind_BD.appointment_prod` AS p
LEFT JOIN `weekly-revenue-integration.Telemind_BD.appointment_authors` AS a USING (ID)
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS cu ON cu.user_id = a.CreatedByUserID
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS mu
  ON mu.user_id = COALESCE(a.LastModifiedByUserID, a.CreatedByUserID);

-- Every modification a job run observed, with the modifier's name.
CREATE OR REPLACE VIEW `weekly-revenue-integration.Telemind_BD.appointment_author_history_named` AS
SELECT
  h.ID,
  h.UpdatedAtUtc,
  h.LastModifiedByUserID,
  u.full_name AS LastModifiedByName,
  u.email AS LastModifiedByEmail,
  h.ProdLastModifiedDate,
  h.captured_at
FROM `weekly-revenue-integration.Telemind_BD.appointment_author_history` AS h
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS u ON u.user_id = h.LastModifiedByUserID;

-- Tebra user IDs seen in appointments but missing from the "Tebra Users" Google Sheet.
CREATE OR REPLACE VIEW `weekly-revenue-integration.Telemind_BD.tebra_users_unmapped` AS
WITH seen AS (
  SELECT CreatedByUserID AS user_id, ID, CreatedAtUtc AS seen_at FROM `weekly-revenue-integration.Telemind_BD.appointment_authors`
  UNION ALL
  SELECT LastModifiedByUserID, ID, UpdatedAtUtc FROM `weekly-revenue-integration.Telemind_BD.appointment_authors`
)
SELECT s.user_id, COUNT(DISTINCT s.ID) AS appointments, MAX(s.seen_at) AS last_seen_utc
FROM seen AS s
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS u USING (user_id)
WHERE s.user_id IS NOT NULL AND u.user_id IS NULL
GROUP BY s.user_id
ORDER BY appointments DESC;
