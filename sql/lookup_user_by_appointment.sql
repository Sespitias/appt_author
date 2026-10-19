-- 1) Tebra user ID of who created / last modified appointments you can identify.
--    Put the appointment IDs you know in `citas`.
DECLARE citas ARRAY<STRING> DEFAULT ['251242', '252274'];

SELECT
  p.ID,
  p.PatientFullName,
  p.StartDate,
  p.ServiceLocationName,
  p.ConfirmationStatus,
  a.CreatedByUserID,         -- who created it
  a.LastModifiedByUserID,    -- last modifier (NULL = not modified since creation)
  a.CreatedAtUtc,
  a.UpdatedAtUtc,
  IF(a.ID IS NULL, 'not processed by the job yet: use scripts/probe_appointment.py', NULL) AS note
FROM `weekly-revenue-integration.Telemind_BD.appointment_prod` AS p
LEFT JOIN `weekly-revenue-integration.Telemind_BD.appointment_authors` AS a USING (ID)
WHERE p.ID IN UNNEST(citas);

-- 2) The other way round: each user ID still missing from the "Tebra Users" Sheet, with the
--    5 most recent appointments it created or modified, to recognise who it is.
SELECT
  u.user_id,
  u.appointments,
  ARRAY_AGG(STRUCT(p.ID, p.PatientFullName, p.StartDate, a.CreatedByUserID = u.user_id AS created_it, a.UpdatedAtUtc)
            ORDER BY a.UpdatedAtUtc DESC LIMIT 5) AS examples
FROM `weekly-revenue-integration.Telemind_BD.tebra_users_unmapped` AS u
JOIN `weekly-revenue-integration.Telemind_BD.appointment_authors` AS a
  ON u.user_id IN (a.CreatedByUserID, a.LastModifiedByUserID)
JOIN `weekly-revenue-integration.Telemind_BD.appointment_prod` AS p USING (ID)
GROUP BY u.user_id, u.appointments
ORDER BY u.appointments DESC;
