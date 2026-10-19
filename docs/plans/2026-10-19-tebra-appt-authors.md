# Autoría de citas Tebra (creador / último modificador) — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tener en BigQuery una versión de `Telemind_BD.appointment_prod` en la que cada cita (ID) lleve el nombre y el email de quien la creó y de quien la modificó por última vez, además de un historial de cada modificación detectada.

**Architecture:** Un Cloud Run **Job** nuevo e independiente (`tebra-appt-authors`) que corre cada 15 min. Llama a Tebra SOAP `GetAppointments` filtrando por `LastModifiedDate` (con un margen de seguridad sobre el último punto procesado) y pide solo ID, fechas, estado e IDs de usuario. Carga los datos en una tabla staging, hace MERGE en `appointment_authors` (una fila por ID) y añade en `appointment_author_history` cada cambio nuevo. Una Google Sheet que mantiene el equipo traduce `user_id` → nombre/email (expuesta en BQ como tabla externa). La vista `appointment_prod_authors` une `appointment_prod` + autores + usuarios, así que siempre refleja lo que el servicio actual ya escribe. El servicio productivo **no se toca**.

**Tech Stack:** Python 3.12, `zeep` (SOAP), `google-cloud-bigquery`, `pytest`, Docker, Cloud Run Jobs, Cloud Scheduler, Secret Manager.

**Spec:** El pedido del usuario en esta conversación + las decisiones que tomó: mapeo en Google Sheet; último modificador + historial; servicio independiente; los nombres de los campos de usuario en Tebra hay que verificarlos (Tarea 1).

## Contexto (lo que hay hoy en BQ)

- Proyecto `weekly-revenue-integration`, dataset `Telemind_BD`, región US.
- `appointment_prod`: 246,612 filas, `ID` STRING único, con `CreatedDate`/`LastModifiedDate` TIMESTAMP. No tiene columnas de usuario.
- El servicio actual (cuenta `gfuentes@telemind.com`) corre unas 5 veces por hora: hace LOAD → `appointment_update` y luego `MERGE INTO appointment_prod` deduplicando por `ID` según `LastModifiedDate DESC`. Seguimos el mismo patrón staging + MERGE.
- Volumen: unas 85 citas modificadas y 13 creadas en el último día.
- En BQ no existe ninguna tabla de usuarios o staff de Tebra.

## Global Constraints

- Proyecto/dataset destino: `weekly-revenue-integration.Telemind_BD`, ubicación `US`.
- No escribir nunca en `appointment_prod` ni en `appointment_update`; el job nuevo solo escribe en sus propias tablas.
- `ID` y todos los `user_id` se guardan como STRING (igual que `appointment_prod.ID`).
- Credenciales de Tebra (customer key, user, password) solo vía Secret Manager. Se reutilizan los mismos secretos que el servicio actual.
- Timestamps se parsean igual que el servicio actual (comparar contra `appointment_prod.LastModifiedDate` para el mismo ID en la Tarea 1).
- Ventana incremental: `from = watermark - 2h`, `to = now`; `watermark = MAX(LastModifiedDate)` en `appointment_authors`.

## Review Focus

1. **user_id vacío, `0` o de sistema** (portal del paciente, reservas online, procesos automáticos): debe quedar NULL o "Sistema/Desconocido" sin romper el load, y no aparecer como usuario sin mapear. → test en Tarea 2 y fila `__SYSTEM__` en la Sheet (Tarea 4).
2. **user_id duplicado en la Sheet**: un JOIN con duplicados multiplicaría las filas de la vista. → la vista deduplica usuarios (Tarea 4) + test de conteo (Tarea 6).
3. **Dos modificaciones de la misma cita entre corridas**: Tebra solo da el último modificador, así que la intermedia se pierde. Se acepta y se documenta; el historial guarda una fila por `(ID, LastModifiedDate)` distinto que se observe. → test de idempotencia en Tarea 3.
4. **Ventanas de backfill demasiado grandes**: Tebra limita o da timeout. → el cliente parte la ventana por día y la reduce a la mitad si hay error (Tarea 2).
5. **Re-ejecución o solape de ventanas**: no debe duplicar historial ni retroceder `appointment_authors` a un estado más viejo. → el MERGE solo actualiza si `source.LastModifiedDate >= target.LastModifiedDate` (Tarea 3).

---

## Estructura de archivos (repo nuevo `tebra-appt-authors/`)

```
src/appt_authors/config.py        # Settings desde env
src/appt_authors/models.py        # dataclass AppointmentAuthor
src/appt_authors/tebra_client.py  # SOAP: fetch + parseo + chunking
src/appt_authors/bq.py            # watermark, load staging, merge, history
src/appt_authors/main.py          # entrypoint: incremental | backfill
scripts/discover_fields.py        # Tarea 1: volcado crudo SOAP
sql/01_tables.sql  sql/02_users_external.sql  sql/03_views.sql
tests/test_tebra_client.py  tests/test_bq_sql.py  tests/fixtures/get_appointments.xml
Dockerfile  requirements.txt  deploy.sh  README.md
```

---

### Tarea 1: Descubrir los campos de usuario en Tebra (spike, bloquea el resto)

**Files:** Create `scripts/discover_fields.py`, `tests/fixtures/get_appointments.xml`, `README.md` (sección "Campos Tebra")

**Interfaces:**
- Produces: los nombres exactos de los campos de creador/modificador (p. ej. `CreatedByUserID` / `LastModifiedByUserID`; por confirmar), escritos en `config.py` como `CREATED_BY_FIELD` y `MODIFIED_BY_FIELD`. También los nombres de los filtros por fecha de modificación de `GetAppointments` (p. ej. `FromLastModifiedDate` / `ToLastModifiedDate`).

- [ ] **Paso 1:** Script que llama a `GetAppointments` para 1 día reciente **sin** `Fields` restringidos (o con todos los del WSDL `AppointmentFieldsToReturn`) y guarda el XML crudo de 5 citas.
  Run: `python scripts/discover_fields.py --date 2026-10-18 --out tests/fixtures/get_appointments.xml`
  Expected: XML con las citas; buscar con `grep -i "user\|createdby\|modifiedby"`.
- [ ] **Paso 2:** Inspeccionar el WSDL (`python -m zeep <wsdl_url>`) y anotar en README los nombres del campo de usuario y de los filtros por fecha de modificación.
- [ ] **Paso 3:** Verificar 3 citas a mano en la UI de Tebra (Appointment → history/audit) y comprobar que el user_id corresponde a la persona correcta. Anotar en README de qué tipo es el ID (usuario de práctica, no provider).
- [ ] **Paso 4 (decisión):** Si Tebra **no** devuelve el modificador, se detiene el plan y se re-evalúa: la alternativa es el reporte de auditoría de Tebra exportado. Si los dos campos existen, continuar.
- [ ] **Paso 5:** Anonimizar el fixture (nombres de paciente → `TEST`) y hacer commit.

### Tarea 2: Cliente Tebra (`fetch_appointment_authors`)

**Files:** Create `src/appt_authors/{config,models,tebra_client}.py`, `tests/test_tebra_client.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) AppointmentAuthor(id: str, created_by_user_id: str | None, last_modified_by_user_id: str | None, created_date: datetime, last_modified_date: datetime, confirmation_status: str | None)`
  - `parse_appointments(xml_or_zeep_result) -> list[AppointmentAuthor]`
  - `fetch_appointment_authors(start: datetime, end: datetime, *, chunk=timedelta(days=1)) -> list[AppointmentAuthor]` (filtra por LastModifiedDate y pide solo los 6 campos)

- [ ] **Paso 1: Tests que fallan**
  - `test_parse_fixture`: con `tests/fixtures/get_appointments.xml`, `len(result) == 5`, todos los `id` son str no vacío y los user_id coinciden con los anotados en la Tarea 1.
  - `test_blank_and_zero_user_ids_become_none`: un `user_id` `""`, `"0"` o ausente → `None`.
  - `test_chunks_window_by_day`: con un SOAP mockeado, la ventana 2026-10-01→2026-10-04 hace 3 llamadas.
  - `test_halves_window_on_error`: si el mock falla con una ventana > 1h, se reintenta con dos mitades; si falla con ≤1h, se lanza la excepción.
  - `test_dedupes_by_id_keeping_latest_modified`.
- [ ] **Paso 2:** `pytest tests/test_tebra_client.py -v` → FAIL (módulo inexistente).
- [ ] **Paso 3:** Implementar. `config.py` lee `TEBRA_CUSTOMER_KEY`, `TEBRA_USER`, `TEBRA_PASSWORD`, `BQ_PROJECT=weekly-revenue-integration`, `BQ_DATASET=Telemind_BD` y las constantes de campos de la Tarea 1.
- [ ] **Paso 4:** `pytest tests/test_tebra_client.py -v` → PASS.
- [ ] **Paso 5:** Commit `feat: tebra client for appointment author fields`.

### Tarea 3: Tablas BQ + escritura (staging → MERGE → historial)

**Files:** Create `sql/01_tables.sql`, `src/appt_authors/bq.py`, `tests/test_bq_sql.py`

**Tablas (`sql/01_tables.sql`, `CREATE TABLE IF NOT EXISTS`):**
- `appointment_authors_staging` — mismas 6 columnas que el dataclass, con nombres `ID, CreatedByUserID, LastModifiedByUserID, CreatedDate, LastModifiedDate, ConfirmationStatus`. Se trunca en cada corrida.
- `appointment_authors` — las mismas columnas + `synced_at TIMESTAMP`. Una fila por `ID`. `CLUSTER BY ID`.
- `appointment_author_history` — `ID, LastModifiedDate, LastModifiedByUserID, ConfirmationStatus, captured_at`. `PARTITION BY DATE(LastModifiedDate) CLUSTER BY ID`. Clave lógica `(ID, LastModifiedDate)`.

**Interfaces:**
- Produces: `get_watermark(client) -> datetime | None`, `load_staging(client, rows: list[AppointmentAuthor]) -> int`, `merge_current(client) -> int`, `append_history(client) -> int`. Los SQL viven como constantes `MERGE_CURRENT_SQL` y `APPEND_HISTORY_SQL` en `bq.py`.

- `MERGE_CURRENT_SQL`: igual que el MERGE actual (dedup `ROW_NUMBER() OVER (PARTITION BY ID ORDER BY LastModifiedDate DESC)`), `WHEN MATCHED AND source.LastModifiedDate >= target.LastModifiedDate THEN UPDATE`, `WHEN NOT MATCHED THEN INSERT`. Ojo: `CreatedByUserID` no se pisa con NULL (`COALESCE(target.CreatedByUserID, source.CreatedByUserID)`).
- `APPEND_HISTORY_SQL`: `INSERT ... SELECT` desde staging `WHERE NOT EXISTS` una fila en history con el mismo `(ID, LastModifiedDate)`.

- [ ] **Paso 1: Tests que fallan** (integración contra un dataset temporal `Telemind_Test_DB`, con fixture que crea las tablas con sufijo aleatorio y las borra al final)
  - `test_merge_inserts_new_ids`
  - `test_merge_does_not_regress_to_older_modification`: carga v2 (más nueva) y luego v1 → la fila queda con v2.
  - `test_merge_keeps_creator_when_source_null`
  - `test_history_is_idempotent`: cargar el mismo lote 2 veces → `COUNT(*)` en history no cambia.
  - `test_history_records_each_distinct_modification`: el mismo ID con dos `LastModifiedDate` → 2 filas.
- [ ] **Paso 2:** `pytest tests/test_bq_sql.py -v` → FAIL.
- [ ] **Paso 3:** Implementar `bq.py` (load con `WRITE_TRUNCATE` y schema explícito).
- [ ] **Paso 4:** `pytest tests/test_bq_sql.py -v` → PASS.
- [ ] **Paso 5:** Commit `feat: bq staging, merge and history for appointment authors`.

### Tarea 4: Mapeo de usuarios (Google Sheet → BQ)

**Files:** Create `sql/02_users_external.sql`, sección "Mantener usuarios" en README

- [ ] **Paso 1:** Crear la Google Sheet "Tebra Users" con la pestaña `users` y estas columnas: `user_id | full_name | email | role | active`. Dar acceso de lectura a la service account del job y a la cuenta que consulta BQ. Primera fila: `__SYSTEM__ | Sistema / Portal | | system | TRUE` (para los IDs que la Tarea 1 identifique como automáticos).
- [ ] **Paso 2:** `sql/02_users_external.sql`: `CREATE OR REPLACE EXTERNAL TABLE Telemind_BD.tebra_users_raw` (`format='GOOGLE_SHEETS'`, `skip_leading_rows=1`, range `users!A:E`, todas las columnas STRING) + `CREATE OR REPLACE VIEW Telemind_BD.tebra_users` que hace `TRIM` del ID, `LOWER(email)` y deduplica por `user_id` (`QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY active DESC) = 1`).
- [ ] **Paso 3:** Rellenar la Sheet con los IDs que aparecen en la Tarea 1 y los que salgan del backfill (Tarea 5) usando la vista de no mapeados (Tarea 6).
- [ ] **Paso 4:** Verificar: `SELECT COUNT(*), COUNT(DISTINCT user_id) FROM Telemind_BD.tebra_users` → los dos números iguales.
- [ ] **Paso 5:** Commit.

### Tarea 5: Entrypoint, backfill y despliegue en Cloud Run Job

**Files:** Create `src/appt_authors/main.py`, `Dockerfile`, `requirements.txt`, `deploy.sh`

**Interfaces:**
- Consumes: `fetch_appointment_authors` (T2), funciones de `bq.py` (T3).
- Produces: CLI `python -m appt_authors.main incremental` y `python -m appt_authors.main backfill --start 2018-09-01 --end 2026-10-19`. Log JSON final con `{"mode","window_start","window_end","fetched","merged","history_added"}`.

- [ ] **Paso 1: Test** `test_incremental_uses_watermark_minus_2h` (mockea `get_watermark` → 10:00, verifica que se llama `fetch` con `start=08:00`). `test_incremental_without_watermark_fails_with_hint` (pide correr backfill primero).
- [ ] **Paso 2:** FAIL → implementar → PASS.
- [ ] **Paso 3:** `deploy.sh`: build en Artifact Registry, `gcloud run jobs deploy tebra-appt-authors --region us-central1 --set-secrets ...` (los mismos secretos del servicio actual), service account con `roles/bigquery.dataEditor` sobre `Telemind_BD` + `roles/bigquery.jobUser` + Drive readonly scope para la Sheet. Cloud Scheduler: `*/15 * * * *` → `jobs.run`.
- [ ] **Paso 4: Backfill.** Ejecutar el job con args `backfill --start 2018-09-01` (el `MIN(CreatedDate)` de prod es 2018-09-06), por mes. Expected: `SELECT COUNT(*) FROM appointment_authors` ≈ 246,612. Nota: para citas antiguas solo se conoce el **último** modificador; el historial completo empieza desde el go-live.
- [ ] **Paso 5:** Activar el scheduler y verificar 2 corridas en los logs de Cloud Run (`history_added` > 0 en horario laboral).
- [ ] **Paso 6:** Commit.

### Tarea 6: Vista final "appointment_prod con autores" + vista de control

**Files:** Create `sql/03_views.sql`

- `Telemind_BD.appointment_prod_authors` (VIEW): `SELECT p.*, a.CreatedByUserID, cu.full_name AS CreatedByName, cu.email AS CreatedByEmail, a.LastModifiedByUserID, mu.full_name AS LastModifiedByName, mu.email AS LastModifiedByEmail, a.synced_at AS AuthorsSyncedAt FROM appointment_prod p LEFT JOIN appointment_authors a USING (ID) LEFT JOIN tebra_users cu ON cu.user_id = a.CreatedByUserID LEFT JOIN tebra_users mu ON mu.user_id = a.LastModifiedByUserID`.
- `Telemind_BD.appointment_author_history_named` (VIEW): history + nombre/email del modificador.
- `Telemind_BD.tebra_users_unmapped` (VIEW): user_ids presentes en authors/history que no están en `tebra_users`, con el conteo de citas y la última fecha en que aparecen.

- [ ] **Paso 1: Checks (SQL de verificación en README):**
  - `SELECT COUNT(*) FROM appointment_prod_authors` = `SELECT COUNT(*) FROM appointment_prod` (sin explosión por el JOIN).
  - `% de filas con CreatedByEmail no nulo` en los últimos 30 días ≥ 95% una vez completada la Sheet.
  - `tebra_users_unmapped` vacío o solo con IDs que ya se están revisando.
  - Que `LastModifiedDate` de prod y de authors coincidan para las citas de hoy (desfase ≤ 15 min).
- [ ] **Paso 2:** Aplicar los SQL y correr los checks.
- [ ] **Paso 3:** Commit `feat: appointment_prod_authors view`.

*(Si se necesita una **tabla** física en lugar de una vista, por ejemplo para Looker Studio o para rendimiento, se añade una scheduled query cada hora `CREATE OR REPLACE TABLE Telemind_BD.appointment_prod_authors_tbl AS SELECT * FROM appointment_prod_authors`. No hace falta por ahora.)*

---

## Verificación de punta a punta

1. `pytest -v` → todo verde.
2. Crear una cita de prueba en Tebra con un usuario conocido y luego modificarla con otro usuario. A los 15–20 min, `SELECT * FROM appointment_prod_authors WHERE ID = '<id>'` muestra los dos nombres y emails correctos, y `appointment_author_history_named` tiene 2 filas.
3. Volver a correr el job a mano 2 veces → los conteos de history no cambian.
4. Revisar `tebra_users_unmapped` cada día (opcional: alerta por email si no está vacío).
