# tebra-appt-authors

Cloud Run Job que registra **quién creó** y **quién modificó por última vez** cada cita de Tebra, y publica en
BigQuery `Telemind_BD.appointment_prod_authors` = `appointment_prod` + nombre y email del creador y del último
modificador. Es independiente del servicio que ya carga `appointment_prod` y nunca escribe en sus tablas.

```
appointment_prod (servicio actual) ──> citas pendientes: nuevas, o cambiadas desde la última vez (las más nuevas primero)
        │                                   │
        │                     Tebra SOAP GetAppointment(ID) ── CreatedBy / UpdatedBy (IDs de usuario)
        │                                   │  (1 llamada cada 0.3 s, máx. 12 min por corrida, cada 15 min)
        │                                   ▼
        │        appointment_authors (1 fila por cita)  +  appointment_author_history (1 fila por modificación)
        ▼                                   │
appointment_prod_authors (vista) <──────────┘ <── tebra_users (vista) <── Google Sheet "Tebra Users"
```

## Por qué GetAppointment

`GetAppointments` (el listado que usa el servicio actual) **no** tiene campos de usuario. Solo `GetAppointment`
(una cita por llamada) devuelve `CreatedBy` / `UpdatedBy`. Esto se verificó contra la API el 2026-10-19.
Tebra limita esa llamada a 1 cada 250 ms, así que:

- **Día a día:** unas 100 citas cambiadas al día se procesan en segundos.
- **Histórico (~246k citas):** se completa solo, unas 2,400 citas por corrida, en ~1–2 días. Las citas de hoy
  siempre van primero, porque el orden es por `LastModifiedDate` descendente.
- **Para revisar una cita a mano:** `python scripts/probe_appointment.py --ids 251242,252274`.

## Significado de los campos

| Campo | Significado |
|---|---|
| `CreatedByUserID` / `CreatedByName` / `CreatedByEmail` | Usuario de Tebra que creó la cita |
| `LastModifiedByUserID` / `…Name` / `…Email` | Último usuario que la modificó. Tebra devuelve `UpdatedBy=0` si no se modificó desde su creación: entonces es el creador |
| `WasCreatedOnline` | Reservada online por el paciente |
| `CreatedAtUtc`, `UpdatedAtUtc` | Fechas reales en UTC según Tebra. Ojo: las fechas de `appointment_prod` son hora local del Pacífico etiquetada como UTC (7–8 h de diferencia) |
| `AuthorsPending` | TRUE si la cita cambió en prod y el job todavía no la reprocesó (≤15 min normalmente) |
| `AuthorsFetchError` | Tebra no devolvió la cita (por ejemplo, borrada). No se reintenta hasta que vuelva a cambiar en prod |

**Historial (`appointment_author_history_named`):** una fila por cada modificación que el job ve. Si una cita cambia
dos veces entre corridas (15 min), solo queda la última. Para las citas históricas solo se conoce su última modificación.

## Mantener usuarios (Google Sheet)

[Tebra Users](https://docs.google.com/spreadsheets/d/10tGKwQq1u7fKJcVStjdFdKdEcCGCQCb1a4B2BM6j9a8/edit),
pestaña `users`, con encabezados `user_id | full_name | email | role | active`.

- **Qué IDs añadir:** cada día revisar `SELECT * FROM Telemind_BD.tebra_users_unmapped` (IDs sin nombre, ordenados
  por cuántas citas tocan) y añadirlos a la Sheet.
- **IDs duplicados:** si un ID aparece dos veces, la vista `tebra_users` se queda con la fila activa, así que no se duplican citas.
- **Acceso:** compartir la Sheet (lector) con quien consulte `appointment_prod_authors`: personas, Looker Studio y
  service accounts. BigQuery la lee con las credenciales de quien hace la consulta; el job no la necesita.

## Despliegue

```bash
# 1. Tablas y vistas (una vez)
bq query --use_legacy_sql=false < sql/01_tables.sql
sed 's|${SHEET_URL}|https://docs.google.com/spreadsheets/d/10tGKwQq1u7fKJcVStjdFdKdEcCGCQCb1a4B2BM6j9a8|' sql/02_users_external.sql | bq query --use_legacy_sql=false
bq query --use_legacy_sql=false < sql/03_views.sql

# 2. Job + scheduler cada 15 min (desde Cloud Shell; usa los secretos TEBRA_* de Secret Manager)
./deploy.sh
```

`deploy.sh` asume que existen el repo `cloud-run` de Artifact Registry, las APIs de Run, Scheduler y Build
habilitadas, y los secretos `TEBRA_CUSTOMER_KEY`, `TEBRA_USER` y `TEBRA_PASSWORD`. Se puede cambiar con
las variables `REPO`, `SECRET_*` y `REGION`.

## Salida de cada corrida (logs de Cloud Run)

```json
{"processed": 2400, "merged": 2400, "history_added": 2390, "fetch_errors": 3, "null_created_by": 0,
 "pending_before": 240000, "pending_after": 237600, "stopped_by": "deadline"}
```

- **`stopped_by`:** `done` (no quedó nada pendiente), `deadline` (se acabó el tiempo; sigue en la próxima corrida)
  o `no_pending`.
- **Errores que detienen la corrida:**
  - credenciales inválidas: se detiene sin escribir nada;
  - ≥20 citas leídas sin ningún `CreatedBy`: la respuesta de Tebra cambió.

## Verificación

```sql
-- sin multiplicación de filas por el JOIN
SELECT (SELECT COUNT(*) FROM Telemind_BD.appointment_prod_authors) = (SELECT COUNT(*) FROM Telemind_BD.appointment_prod);
-- progreso del histórico
SELECT COUNTIF(CreatedByUserID IS NULL AND AuthorsFetchError IS NULL) AS sin_autor, COUNT(*) AS total
FROM Telemind_BD.appointment_prod_authors;
-- cobertura de nombres de los últimos 30 días (objetivo ≥ 95% con la Sheet completa)
SELECT COUNTIF(CreatedByEmail IS NOT NULL) / COUNT(*) FROM Telemind_BD.appointment_prod_authors
WHERE CreatedAtUtc >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);
```

## Tests

```bash
pip install -r requirements.txt
pytest -v                       # unitarios (fixtures = respuestas reales de Tebra sin datos de pacientes)
RUN_BQ_TESTS=1 pytest -v -m bq  # integración contra Telemind_Test_DB (requiere credenciales GCP)
```
