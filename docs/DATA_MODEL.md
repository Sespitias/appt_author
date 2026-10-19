# Modelo de datos: autoría de citas Tebra

Contexto para definir reportes sobre **quién agenda y quién modifica** cada cita.
Proyecto BigQuery `weekly-revenue-integration`, dataset `Telemind_BD`, región US.

## Cómo fluyen los datos

```
Tebra ──(servicio existente, cada ~12 min)──> appointment_prod             (1 fila por cita)
Tebra GetAppointment ──(job tebra-appt-authors, cada 15 min)──> appointment_authors          (1 fila por cita)
                                                            └──> appointment_author_history  (1 fila por modificación vista)
Google Sheet "Tebra Users" ──> tebra_users_raw (tabla externa) ──> tebra_users (vista, 1 fila por user_id)

appointment_prod + appointment_authors + tebra_users ──> appointment_prod_authors   ← VISTA PRINCIPAL PARA REPORTES
```

**Clave de unión:** `ID` (STRING), el ID de cita de Tebra. Los usuarios se unen por `user_id` (STRING).

## Qué usar para reportar

| Objeto | Tipo | Grano | Úsalo para |
|---|---|---|---|
| `appointment_prod_authors` | vista | 1 fila por cita | **Casi todo.** Todo `appointment_prod` + quién creó / último que modificó, con nombre y email |
| `appointment_author_history_named` | vista | 1 fila por modificación observada | Actividad por agente en el tiempo (cuántas modificaciones hizo cada uno por día) |
| `tebra_users` | vista | 1 fila por usuario | Catálogo de agentes (nombre, email, rol, activo) |
| `tebra_users_unmapped` | vista | 1 fila por user_id sin nombre | Control: IDs que faltan en la Sheet |
| `appointment_authors` | tabla | 1 fila por cita | Datos crudos del job (IDs, sin nombres) |
| `appointment_author_history` | tabla | 1 fila por (cita, modificación) | Historial crudo |

Las vistas que llevan nombres leen la Google Sheet: quien consulte (persona, Looker Studio, service account)
necesita **acceso de lectura a la Sheet "Tebra Users"**.

## `appointment_prod_authors` (vista principal)

Todas las columnas de `appointment_prod`, más las de autoría.

**Columnas de `appointment_prod` (las mantiene el servicio existente):**

| Columna | Tipo | Nota |
|---|---|---|
| ID | STRING | ID de cita. Las citas recurrentes tienen sufijo, p. ej. `163379KKF` |
| ConfirmationStatus | STRING | Scheduled, Confirmed, Check-Out, Cancelled, Rescheduled, No Show, Deleted… |
| CreatedDate, LastModifiedDate | TIMESTAMP | **Hora local del Pacífico etiquetada como UTC** |
| StartDate, EndDate | TIMESTAMP | Misma convención (hora del Pacífico) |
| StartOnlyDate | DATE | Fecha de la cita |
| PatientID, PatientFullName | STRING | |
| PatientCaseID, PatientCaseName, PatientCasePayerScenario, CaseNameID | STRING | |
| ServiceLocationName, Provider, Service, AppointmentReason1 | STRING | |
| Done | INTEGER | |
| WeekDate, MonthDate, CreationWeekDate | STRING | |
| Month, Week | INTEGER | |
| Time | FLOAT | |

**Columnas de autoría (las agrega el job):**

| Columna | Tipo | Significado |
|---|---|---|
| CreatedByUserID | STRING | Usuario de Tebra que **creó/agendó** la cita |
| CreatedByName, CreatedByEmail | STRING | Desde la Sheet. NULL si el ID aún no tiene nombre |
| LastModifiedByUserID | STRING | Último usuario que la modificó. Si nadie la modificó desde la creación, es el creador |
| LastModifiedByName, LastModifiedByEmail | STRING | Desde la Sheet |
| WasCreatedOnline | BOOL | TRUE = la reservó el paciente online (no hay agente) |
| CreatedAtUtc, UpdatedAtUtc | TIMESTAMP | Creación y última modificación según Tebra, en **UTC real** |
| AuthorsPending | BOOL | TRUE = la cita cambió en prod y el job aún no la reprocesó (≤15 min) |
| AuthorsFetchError | STRING | Tebra no devolvió la cita (borrada o no autorizada) |
| AuthorsSyncedAt | TIMESTAMP | Cuándo la procesó el job |

**Cobertura:** las citas que el job todavía no procesó tienen `CreatedByUserID` NULL y `AuthorsSyncedAt` NULL.
El histórico (~246k citas) se completa solo en ~2 días desde el 2026-10-19, de las más nuevas a las más viejas.
Para medir avance: `COUNTIF(AuthorsSyncedAt IS NOT NULL) / COUNT(*)`.

## `appointment_author_history_named`

| Columna | Tipo | Significado |
|---|---|---|
| ID | STRING | Cita |
| UpdatedAtUtc | TIMESTAMP | Momento de la modificación (UTC real). Partición por día |
| LastModifiedByUserID | STRING | Quién modificó. NULL = modificación sin usuario (no modificada desde la creación) |
| LastModifiedByName, LastModifiedByEmail | STRING | Desde la Sheet |
| ProdLastModifiedDate | TIMESTAMP | `LastModifiedDate` de prod en ese momento |
| captured_at | TIMESTAMP | Cuándo la vio el job |

**Limitaciones del historial:**
- Tebra solo informa el último modificador. Si una cita cambia dos veces en menos de 15 min, solo queda el último cambio.
- Para las citas históricas cargadas en el backfill, el historial tiene solo su última modificación. El historial
  completo empieza el 2026-10-19.

## `tebra_users`, la Google Sheet "Tebra Users"

Sheet: https://docs.google.com/spreadsheets/d/10tGKwQq1u7fKJcVStjdFdKdEcCGCQCb1a4B2BM6j9a8/edit (pestaña `users`).

| Columna | Tipo | Nota |
|---|---|---|
| user_id | STRING | ID de usuario de Tebra (`__SYSTEM__` = fila de referencia) |
| full_name | STRING | Nombre del agente |
| email | STRING | En minúsculas |
| role | STRING | p. ej. `Agent` |
| active | BOOL | TRUE si el texto es TRUE/YES/SI/1 o está vacío |

Si un `user_id` aparece dos veces, la vista se queda con la fila activa. Los cambios en la Sheet se ven en BigQuery al instante.

## Reglas para reportes

- **"Agendada por" = `CreatedByName`.** "Último cambio por" = `LastModifiedByName`.
- **Fechas:** para agrupar por día de agendamiento en hora local usa `DATE(CreatedAtUtc, 'America/Los_Angeles')`.
  Las fechas de prod (`CreatedDate`, `StartDate`) ya están en hora del Pacífico; no las conviertas.
- **Citas sin agente:** excluye o separa `WasCreatedOnline = TRUE` (reservas online) y `CreatedByUserID IS NULL`
  (aún no procesadas).
- **Citas recurrentes:** las ocurrencias (`ID` con sufijo) heredan el autor de la serie, así que pueden inflar los
  conteos de quien creó la serie. Para contar agendamientos reales, filtra `REGEXP_CONTAINS(ID, r'^[0-9]+$')`.

## Consultas de ejemplo

```sql
-- Citas agendadas por agente y día (hora del Pacífico), últimos 30 días
SELECT DATE(CreatedAtUtc, 'America/Los_Angeles') AS dia, CreatedByName AS agente, COUNT(*) AS citas_agendadas
FROM `weekly-revenue-integration.Telemind_BD.appointment_prod_authors`
WHERE CreatedAtUtc >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND NOT IFNULL(WasCreatedOnline, FALSE) AND REGEXP_CONTAINS(ID, r'^[0-9]+$')
GROUP BY dia, agente ORDER BY dia DESC, citas_agendadas DESC;

-- Resultado de las citas que agendó cada agente (calidad del agendamiento)
SELECT CreatedByName AS agente, ConfirmationStatus, COUNT(*) AS citas
FROM `weekly-revenue-integration.Telemind_BD.appointment_prod_authors`
WHERE StartOnlyDate BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND CURRENT_DATE()
  AND CreatedByName IS NOT NULL
GROUP BY agente, ConfirmationStatus ORDER BY agente, citas DESC;

-- Cancelaciones / reprogramaciones: quién hizo el último cambio
SELECT LastModifiedByName AS agente, ConfirmationStatus, COUNT(*) AS citas
FROM `weekly-revenue-integration.Telemind_BD.appointment_prod_authors`
WHERE ConfirmationStatus IN ('Cancelled', 'Rescheduled', 'No Show')
  AND DATE(UpdatedAtUtc, 'America/Los_Angeles') >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
GROUP BY agente, ConfirmationStatus ORDER BY citas DESC;

-- Modificaciones por agente y día (actividad), desde el historial
SELECT DATE(UpdatedAtUtc, 'America/Los_Angeles') AS dia, LastModifiedByName AS agente, COUNT(*) AS modificaciones
FROM `weekly-revenue-integration.Telemind_BD.appointment_author_history_named`
WHERE UpdatedAtUtc >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY) AND LastModifiedByUserID IS NOT NULL
GROUP BY dia, agente ORDER BY dia DESC, modificaciones DESC;

-- Control: IDs sin nombre en la Sheet
SELECT * FROM `weekly-revenue-integration.Telemind_BD.tebra_users_unmapped`;
```
