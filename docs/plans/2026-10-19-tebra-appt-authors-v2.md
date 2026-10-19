# Autoría de citas Tebra — Plan v2 (rediseño tras confirmar la API real)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

## Contexto

La v1 ya está implementada en `/home/user/tebra-appt-authors` (rama `feat/appt-authors`, 8 commits, 18 tests). Partía de un supuesto que la API real desmiente.

**Lo que se confirmó contra Tebra el 2026-10-19** (con WSDL + llamadas reales de solo lectura):
- **`GetAppointments`** (el listado) **no tiene** campos de usuario: 75 campos, ninguno de creador/modificador.
- **`GetAppointment(AppointmentId)`** (en singular) devuelve `CreatedBy` y `UpdatedBy` (IDs de usuario int), `CreatedAt` / `UpdatedAt` (UTC real), `WasCreatedOnline` e `IsDeleted`.
  - Ejemplo 251242 → CreatedBy 984392, UpdatedBy 832888.
  - Ejemplo 252274 → CreatedBy 832888, UpdatedBy **0** (nunca modificada).
- **Límite de la API:** `GetAppointment` 250 ms entre llamadas (4/s).
- **`appointment_prod` está 7 h por detrás de UTC:** guarda hora local del Pacífico etiquetada como UTC. La autoría se une por `ID`, así que la zona horaria deja de importar.
- **Decisión del usuario:** backfill de **todo** el histórico (246,612 citas), automático, de las más nuevas a las más viejas.

**Rediseño:** `appointment_prod` (que el servicio actual ya mantiene) es la fuente de **qué citas** procesar. Tebra `GetAppointment` es la fuente de **quién**. Cada corrida (cada 15 min, máximo 12 min de trabajo) hace esto:
1. Busca las citas pendientes: están en prod pero no en `appointment_authors`, o su `LastModifiedDate` en prod es más nuevo que el guardado.
2. Las ordena de la más nueva a la más vieja.
3. Llama a `GetAppointment` respetando el límite de la API.
4. Guarda cada 500 citas.

Los cambios del día siempre salen primero. El histórico se completa solo en unas 90 corridas (~1–2 días). Desaparecen el watermark, la zona horaria, el troceo de ventanas y los nombres de campo supuestos.

## Global Constraints

- Proyecto `weekly-revenue-integration`, dataset `Telemind_BD`, ubicación `US`. Nunca se escribe en `appointment_prod` ni en `appointment_update`.
- `ID` y user IDs se guardan como STRING. `UpdatedBy`/`CreatedBy` = 0 o vacío → NULL.
- Intervalo mínimo entre llamadas `GetAppointment` = 0.25 s (`TEBRA_MIN_INTERVAL`, default `0.30`). Presupuesto por corrida `MAX_RUN_SECONDS` default `720`. Flush a BigQuery cada `BATCH_SIZE` default `500`.
- Credenciales solo por variables de entorno o Secret Manager (`TEBRA_CUSTOMER_KEY`, `TEBRA_USER`, `TEBRA_PASSWORD`). Nunca en archivos, logs ni fixtures.
- Fixtures sin datos de pacientes: solo IDs de cita/usuario, fechas, estado y booleanos.

## Review Focus

1. **Cita borrada o inexistente en Tebra** (`IsError` / `IsDeleted`): se guarda con `FetchError` / `IsDeleted` para que no quede pendiente para siempre. → test en Tarea 2.
2. **Errores de límite de la API o transitorios** (timeout, 5xx): reintento con backoff (3 intentos). Si persiste, la corrida corta y guarda lo hecho, sin perder progreso. → test en Tarea 2.
3. **Credenciales inválidas:** fallar en la primera llamada con un mensaje claro, sin 2,800 errores. → test en Tarea 2.
4. **Cita modificada otra vez mientras se procesa:** el MERGE nunca retrocede (compara `ProdLastModifiedDate`). El historial usa `UpdatedAtUtc` real de Tebra como clave. → tests BQ en Tarea 3.
5. **Corridas que se solapan** (backlog grande): presupuesto de 12 min < 15 min de intervalo, staging por corrida y Cloud Run Job con `parallelism=1`. → test de deadline en Tarea 4.

---

## Archivos

Se reutiliza lo existente: `bq.py` (`Tables.for_run`, `load_staging`, `drop_staging`), la Sheet + `sql/02_users_external.sql` (sin cambios) y el patrón de tests.

```
src/appt_authors/models.py        # AppointmentAuthor v2
src/appt_authors/config.py        # quita field/filter/timezone; añade MIN_INTERVAL, MAX_RUN_SECONDS, BATCH_SIZE
src/appt_authors/tebra_client.py  # GetAppointment por ID + throttle + retries (reemplaza GetAppointments)
src/appt_authors/bq.py            # PENDING_SQL/get_pending, MERGE/HISTORY v2
src/appt_authors/main.py          # un solo modo: run() con deadline y flush por lotes
scripts/probe_appointment.py      # reemplaza discover_fields.py: imprime autoría de IDs dados (sin PHI)
sql/01_tables.sql  sql/03_views.sql   # esquema v2 + vistas v2
tests/fixtures/get_appointment*.xml   # respuestas reales saneadas (OK, UpdatedBy=0, error)
```

---

### Tarea 1: Modelo + parser de `GetAppointment`

**Interfaces — Produces:**
- `AppointmentAuthor(id: str, created_by_user_id: str|None, last_modified_by_user_id: str|None, created_at_utc: datetime|None, updated_at_utc: datetime|None, was_created_online: bool|None, is_deleted: bool|None, prod_last_modified: datetime|None = None, fetch_error: str|None = None)`
- `parse_get_appointment(xml: bytes, appointment_id: str) -> AppointmentAuthor`. Si la respuesta trae `IsError` con un error de autenticación o seguridad (`SecurityResponse.Authenticated/Authorized=false`), lanza `TebraAuthError`. Con otro `IsError`, devuelve el objeto con `fetch_error` = mensaje.

- [ ] Capturar 3 respuestas XML reales con `scripts/probe_appointment.py --ids 251242,252274 --save-xml` y una con un ID inexistente (`--ids 1`). Sanearlas: borrar `PatientSummary`/`PatientSummaries`/`Notes` y guardarlas en `tests/fixtures/`.
- [ ] Tests (RED):
  - `test_parse_created_and_updated_by`: 251242 → `"984392"`, `"832888"`, `updated_at_utc == 2026-10-19 15:50:01Z`.
  - `test_updated_by_zero_becomes_none`: 252274 → `last_modified_by_user_id is None`.
  - `test_parse_not_found_sets_fetch_error`.
  - `test_auth_failure_raises`: fixture sintético con `Authenticated=false`.
- [ ] Implementar (reusar `_local`, `_child_text`, `_user_id` de `tebra_client.py`; ISO con `Z` → `datetime.fromisoformat`) → GREEN → commit.

### Tarea 2: Cliente con límite de la API y reintentos

**Interfaces — Produces:**
- `TebraClient.get_appointment(appointment_id: str) -> bytes`: XML crudo, con `Transport(timeout=60, operation_timeout=300)` como ya está.
- `fetch_authors(ids: Iterable[str], *, call: Callable[[str], AppointmentAuthor], deadline: float, min_interval: float, sleep=time.sleep, clock=time.monotonic) -> Iterator[AppointmentAuthor]`

- [ ] Tests (RED):
  - `test_respects_min_interval`: con reloj y sleep falsos, 3 IDs → sleeps que suman ≥ 2×min_interval.
  - `test_stops_at_deadline`: devuelve solo los IDs procesados antes del deadline.
  - `test_retries_transient_then_succeeds`: 2 fallos y luego OK.
  - `test_gives_up_after_3_transient_errors_without_losing_previous`: se cortan los resultados y la excepción se propaga después de devolver los anteriores.
  - `test_auth_error_not_retried`.
- [ ] Implementar. Borrar `parse_appointments`, `fetch_appointment_authors`, `_fetch_window`, `get_modified` y sus tests y fixture viejos. → GREEN → commit.

### Tarea 3: BigQuery v2 (pendientes, MERGE, historial)

**Esquema** (`sql/01_tables.sql`, reemplaza el v1; las tablas v1 nunca se crearon en prod):
- `appointment_authors`: `ID, CreatedByUserID, LastModifiedByUserID, CreatedAtUtc, UpdatedAtUtc, WasCreatedOnline BOOL, IsDeleted BOOL, ProdLastModifiedDate, FetchError, synced_at`. `CLUSTER BY ID`.
- `appointment_author_history`: `ID, UpdatedAtUtc NOT NULL, LastModifiedByUserID, ProdLastModifiedDate, captured_at`. `PARTITION BY DATE(UpdatedAtUtc)`, clave `(ID, UpdatedAtUtc)`.

**Interfaces — Produces:**
- `get_pending(client, tables, limit: int) -> list[tuple[str, datetime]]`, con `PENDING_SQL`:
  ```sql
  SELECT p.ID, p.LastModifiedDate FROM appointment_prod p LEFT JOIN appointment_authors a USING (ID)
  WHERE a.ID IS NULL OR a.ProdLastModifiedDate < p.LastModifiedDate
  ORDER BY p.LastModifiedDate DESC LIMIT @limit
  ```
- `count_pending(client, tables) -> int`.
- `MERGE_CURRENT_SQL`: actualiza si `S.ProdLastModifiedDate >= T.ProdLastModifiedDate`. Usa `COALESCE(S.CreatedByUserID, T.CreatedByUserID)`, sin cambios respecto a v1.
- `APPEND_HISTORY_SQL`: `NOT EXISTS` por `(ID, UpdatedAtUtc)`, solo filas con `UpdatedAtUtc IS NOT NULL` y `FetchError IS NULL`, con predicado de partición `H.UpdatedAtUtc >= (SELECT MIN(UpdatedAtUtc) FROM staging)`.
- `Tables` gana `prod: str`, para que los tests apunten a una copia.

- [ ] Script de ASSERTs vía MCP en `Telemind_Test_DB` (patrón v1), con un `prod` de prueba de 4 filas:
  - pendientes = nuevos + cambiados, ordenados DESC;
  - el MERGE no retrocede;
  - el historial es idempotente;
  - una fila con `FetchError` deja de estar pendiente.

  Borrar los objetos de prueba. Actualizar `tests/test_bq_sql.py` (marker `bq`) con los mismos casos.
- [ ] Commit.

### Tarea 4: `main.run()` v2

**Interfaces — Produces:**
- CLI `python -m appt_authors.main [--max-seconds N] [--limit N]`.
- `run(*, client=None, tables=None, call=None, now=time.monotonic, max_seconds=720, batch_size=500) -> dict`.
- JSON de salida: `{"processed","merged","history_added","fetch_errors","null_created_by","pending_before","pending_after","stopped_by"}`.

- [ ] Tests (RED):
  - `test_flushes_every_batch_size`: 1,200 IDs, batch 500 → 3 loads.
  - `test_partial_progress_saved_on_tebra_failure`.
  - `test_stops_by_deadline_and_reports_it`.
  - `test_no_pending_does_nothing`.
  - `test_null_author_guard`: ≥20 sin `CreatedBy` y sin `fetch_error` → falla.
  - Se mantienen los de staging por corrida (adaptados).
- [ ] Implementar, reusando `Tables.for_run` / `drop_staging` / `_check_authors`. Quitar watermark, backfill y `_month_windows`. → GREEN → commit.

### Tarea 5: Vistas v2 + probe + deploy + README

- [ ] `sql/03_views.sql`:
  - `appointment_prod_authors` añade `CreatedByName/Email`, `LastModifiedByName/Email`, `WasCreatedOnline`, `CreatedAtUtc`, `UpdatedAtUtc`.
  - El último modificador efectivo es `COALESCE(a.LastModifiedByUserID, a.CreatedByUserID)`, porque `UpdatedBy=0` significa que la cita no se modificó desde su creación.
  - `appointment_author_history_named` y `tebra_users_unmapped` se adaptan a las columnas nuevas.
  - Validar con un script de ASSERTs en `Telemind_Test_DB` contra el prod real: sin multiplicación de filas, nombres resueltos y modificador efectivo correcto.
- [ ] `scripts/probe_appointment.py --ids a,b [--save-xml DIR]`: imprime solo campos de autoría.
- [ ] `deploy.sh`:
  - quitar las env vars de campos/filtros/zona horaria;
  - `--task-timeout 900 --parallelism 1 --max-retries 0`;
  - scheduler `*/15` **activo** (ya no hay backfill aparte).
- [ ] README: nuevo flujo, Sheet, verificación y notas:
  - `UpdatedBy=0` → modificador = creador;
  - `WasCreatedOnline` = reserva online del paciente;
  - prod está en hora local del Pacífico y las columnas `*Utc` en UTC real.
- [ ] `pytest` en verde. Revisión final con un revisor nuevo y una pasada de correcciones. Commit.

### Tarea 6: Puesta en producción (requiere al usuario para gcloud)

- [ ] Crear tablas y vistas en `Telemind_BD` vía MCP de BigQuery (`01`, `02` con la URL de la Sheet, `03`).
- [ ] Prueba de punta a punta sin desplegar:
  - correr `fetch_authors` localmente sobre 50 IDs pendientes (credenciales Tebra del entorno);
  - escribir en `Telemind_BD` vía MCP (el cliente Python de BQ no tiene credenciales aquí);
  - comprobar en `appointment_prod_authors` que los 50 tienen `CreatedByUserID`.
- [ ] Deploy: el usuario corre `./deploy.sh` en Cloud Shell. En este contenedor `CLOUDSDK_AUTH_ACCESS_TOKEN` fija un token que el login interactivo no puede reemplazar.
- [ ] Rellenar la Sheet con los IDs de `tebra_users_unmapped`, que se poblará con la primera corrida.

## Verificación de punta a punta

1. `pytest -v` en verde (unitarios). Scripts de ASSERTs de BQ (Tareas 3 y 5) → `ALL ASSERTS PASSED`.
2. `python scripts/probe_appointment.py --ids 251242,252274` → 984392/832888 y 832888/None.
3. Después de la Tarea 6:
   - `SELECT COUNT(*) FROM appointment_prod_authors` = `COUNT(*)` de `appointment_prod`;
   - `pending_after` baja corrida a corrida hasta ~0;
   - las citas de hoy tienen `CreatedByEmail` una vez completada la Sheet.
4. Crear y modificar una cita de prueba en Tebra con dos usuarios. En ≤15 min ambos nombres aparecen en la vista y el historial tiene 2 filas.
