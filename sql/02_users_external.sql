-- Google Sheet "Tebra Users" (tab `users`, header row: user_id | full_name | email | role | active).
-- Render with: sed 's|${SHEET_URL}|<sheet url>|' sql/02_users_external.sql | bq query --use_legacy_sql=false
CREATE OR REPLACE EXTERNAL TABLE `weekly-revenue-integration.Telemind_BD.tebra_users_raw` (
  user_id STRING,
  full_name STRING,
  email STRING,
  role STRING,
  active STRING
)
OPTIONS (
  format = 'GOOGLE_SHEETS',
  uris = ['${SHEET_URL}'],
  sheet_range = 'users!A:E',
  skip_leading_rows = 1
);

-- One row per user_id: trimmed ID, lower-cased email; on duplicates the active row wins.
CREATE OR REPLACE VIEW `weekly-revenue-integration.Telemind_BD.tebra_users` AS
SELECT
  TRIM(user_id) AS user_id,
  NULLIF(TRIM(full_name), '') AS full_name,
  NULLIF(LOWER(TRIM(email)), '') AS email,
  NULLIF(TRIM(role), '') AS role,
  UPPER(TRIM(IFNULL(active, 'TRUE'))) IN ('TRUE', 'YES', 'SI', 'SÍ', '1') AS active
FROM `weekly-revenue-integration.Telemind_BD.tebra_users_raw`
WHERE NULLIF(TRIM(user_id), '') IS NOT NULL
-- `active` / `full_name` here are the SELECT aliases (BOOL / trimmed name).
QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY active DESC, full_name) = 1;
