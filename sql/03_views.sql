-- appointment_prod + creator / last modifier names. Always as fresh as appointment_prod itself;
-- author columns lag by at most one job run (15 min). tebra_users is unique per user_id, so no fan-out.
CREATE OR REPLACE VIEW `weekly-revenue-integration.Telemind_BD.appointment_prod_authors` AS
SELECT
  p.*,
  a.CreatedByUserID,
  cu.full_name AS CreatedByName,
  cu.email AS CreatedByEmail,
  a.LastModifiedByUserID,
  mu.full_name AS LastModifiedByName,
  mu.email AS LastModifiedByEmail,
  a.LastModifiedDate AS AuthorsLastModifiedDate,
  a.synced_at AS AuthorsSyncedAt
FROM `weekly-revenue-integration.Telemind_BD.appointment_prod` AS p
LEFT JOIN `weekly-revenue-integration.Telemind_BD.appointment_authors` AS a USING (ID)
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS cu ON cu.user_id = a.CreatedByUserID
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS mu ON mu.user_id = a.LastModifiedByUserID;

-- Every observed modification with the modifier's name.
CREATE OR REPLACE VIEW `weekly-revenue-integration.Telemind_BD.appointment_author_history_named` AS
SELECT
  h.ID,
  h.LastModifiedDate,
  h.LastModifiedByUserID,
  u.full_name AS LastModifiedByName,
  u.email AS LastModifiedByEmail,
  h.ConfirmationStatus,
  h.captured_at
FROM `weekly-revenue-integration.Telemind_BD.appointment_author_history` AS h
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS u ON u.user_id = h.LastModifiedByUserID;

-- Tebra user IDs seen in appointments but missing from the "Tebra Users" Google Sheet.
CREATE OR REPLACE VIEW `weekly-revenue-integration.Telemind_BD.tebra_users_unmapped` AS
WITH seen AS (
  SELECT CreatedByUserID AS user_id, ID, CreatedDate AS seen_at FROM `weekly-revenue-integration.Telemind_BD.appointment_authors`
  UNION ALL
  SELECT LastModifiedByUserID, ID, LastModifiedDate FROM `weekly-revenue-integration.Telemind_BD.appointment_authors`
  UNION ALL
  SELECT LastModifiedByUserID, ID, LastModifiedDate FROM `weekly-revenue-integration.Telemind_BD.appointment_author_history`
)
SELECT s.user_id, COUNT(DISTINCT s.ID) AS appointments, MAX(s.seen_at) AS last_seen
FROM seen AS s
LEFT JOIN `weekly-revenue-integration.Telemind_BD.tebra_users` AS u USING (user_id)
WHERE s.user_id IS NOT NULL AND u.user_id IS NULL
GROUP BY s.user_id
ORDER BY appointments DESC;
