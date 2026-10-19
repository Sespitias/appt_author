# tebra-appt-authors

Cloud Run Job que registra **quién creó** y **quién modificó por última vez** cada cita de Tebra,
y publica en BigQuery `Telemind_BD.appointment_prod_authors` (= `appointment_prod` + nombre/email
del creador y del último modificador). Es independiente del servicio que ya carga `appointment_prod`.

```
Tebra SOAP GetAppointments (filtrado por LastModifiedDate)
   └─> appointment_authors_staging ──MERGE──> appointment_authors        (1 fila por ID)
                                   └─INSERT─> appointment_author_history (1 fila por ID+LastModifiedDate)
Google Sheet "Tebra Users" ─> tebra_users_raw (externa) ─> tebra_users (vista dedup)
appointment_prod + appointment_authors + tebra_users ─> appointment_prod_authors (vista)
```

## Campos Tebra (Tarea 1: hay que confirmarlos antes del primer deploy)

Los nombres de los campos y filtros se pueden configurar con variables de entorno.
Los valores por defecto son **suposiciones** basadas en la convención de nombres de Kareo:

| Variable | Default | Confirmado |
|---|---|---|
| `TEBRA_CREATED_BY_FIELD` | `CreatedByUserID` | ☐ |
| `TEBRA_MODIFIED_BY_FIELD` | `LastModifiedByUserID` | ☐ |
| `TEBRA_FROM_MODIFIED_FILTER` | `FromLastModifiedDate` | ☐ |
| `TEBRA_TO_MODIFIED_FILTER` | `ToLastModifiedDate` | ☐ |

Procedimiento:

1. `pip install -r requirements.txt`
2. `TEBRA_CUSTOMER_KEY=... TEBRA_USER=... TEBRA_PASSWORD=... python scripts/discover_fields.py --date 2026-10-18 --out /tmp/discovery.xml`
   El script imprime los elementos del WSDL relacionados con usuarios y fechas, y los campos de usuario presentes en la respuesta real.
3. Ajustar las 4 variables (en `deploy.sh`) si los nombres no coinciden con los defaults.
4. Abrir 3 de esas citas en la UI de Tebra (historial/auditoría de la cita) y comprobar que el ID corresponde a la persona correcta.
5. Anonimizar el XML (nombres de paciente → `TEST`), copiarlo a `tests/fixtures/get_appointments.xml`, actualizar
   los valores esperados en `tests/test_tebra_client.py::test_parse_fixture` y correr `pytest`.
6. **Zona horaria (obligatorio antes del deploy):** después de una primera corrida corta
   (`backfill --start <ayer> --end <hoy>`), comprobar que las horas coinciden con `appointment_prod`:
   ```sql
   SELECT COUNTIF(a.LastModifiedDate != p.LastModifiedDate) AS distintas, COUNT(*) AS total,
          APPROX_TOP_COUNT(TIMESTAMP_DIFF(a.LastModifiedDate, p.LastModifiedDate, HOUR), 3) AS desfase_horas
   FROM Telemind_BD.appointment_authors a JOIN Telemind_BD.appointment_prod p USING (ID)
   WHERE a.LastModifiedDate >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
   ```
   Si `desfase_horas` es constante (por ejemplo 4 o 5), definir `TEBRA_TIMEZONE` (por ejemplo `America/New_York`) y repetir.
   El default `UTC` guarda la hora tal como llega.

**Si Tebra no devuelve el modificador:** se detiene el despliegue y se evalúa la alternativa (reporte de auditoría exportado de Tebra).

## Mantener usuarios (Google Sheet)

Sheet **"Tebra Users"**, pestaña `users`, encabezados en la fila 1:

| user_id | full_name | email | role | active |
|---|---|---|---|---|
| `__SYSTEM__` | Sistema / Portal | | system | TRUE |

- Compartirla (lector) con quien consulte `appointment_prod_authors` en BigQuery (personas, Looker Studio, service accounts). BigQuery lee la Sheet con las credenciales de quien hace la consulta; el job no la lee.
- Cada día revisar `SELECT * FROM Telemind_BD.tebra_users_unmapped` y añadir los IDs que falten.
- Si un ID aparece dos veces, la vista `tebra_users` se queda con la fila activa (no duplica citas).
- Las citas sin ID de usuario (vacío o `0`: portal, procesos automáticos) quedan con nombre NULL. La fila `__SYSTEM__`
  es solo una referencia. Si el portal usa un ID real distinto de 0, aparecerá en `tebra_users_unmapped`; añádelo con ese ID.

El job falla a propósito si una tanda de ≥20 citas llega sin ningún ID de creador ni de modificador
(eso indica que los nombres de los campos están mal configurados). Cada corrida imprime `null_created_by` / `null_modified_by`.

## Despliegue

```bash
# 1. Tablas y vistas (una vez)
bq query --use_legacy_sql=false < sql/01_tables.sql
sed 's|${SHEET_URL}|https://docs.google.com/spreadsheets/d/10tGKwQq1u7fKJcVStjdFdKdEcCGCQCb1a4B2BM6j9a8|' sql/02_users_external.sql | bq query --use_legacy_sql=false
bq query --use_legacy_sql=false < sql/03_views.sql

# 2. Job + scheduler (*/15)
./deploy.sh

# 3. Backfill (una vez; internamente va mes a mes). Por tramos, UNO DESPUÉS DE OTRO (--wait):
gcloud run jobs execute tebra-appt-authors --region us-central1 --wait --args="backfill,--start,2018-09-01,--end,2020-01-01"
# ... repetir por tramos hasta hoy; luego activar el scheduler (se crea en pausa):
gcloud scheduler jobs resume tebra-appt-authors-15m --location us-central1
```

Nota: el backfill solo puede recuperar el **último** modificador de cada cita histórica.
El historial completo de modificaciones empieza el día en que se activa el job, y si una cita
cambia dos veces entre corridas (15 min) solo se registra el último cambio.

## Verificación

```sql
-- sin explosión por el JOIN
SELECT (SELECT COUNT(*) FROM Telemind_BD.appointment_prod_authors) = (SELECT COUNT(*) FROM Telemind_BD.appointment_prod);
-- cobertura de los últimos 30 días (objetivo ≥ 95% una vez completa la Sheet)
SELECT COUNTIF(CreatedByEmail IS NOT NULL) / COUNT(*) FROM Telemind_BD.appointment_prod_authors
WHERE CreatedDate >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);
-- IDs sin mapear
SELECT * FROM Telemind_BD.tebra_users_unmapped;
-- desfase prod vs authors hoy
SELECT COUNTIF(a.LastModifiedDate IS NULL OR a.LastModifiedDate < p.LastModifiedDate) AS atrasadas
FROM Telemind_BD.appointment_prod p LEFT JOIN Telemind_BD.appointment_authors a USING (ID)
WHERE DATE(p.LastModifiedDate) = CURRENT_DATE();
```

## Tests

```bash
pip install -r requirements.txt
pytest -v                      # unit tests
RUN_BQ_TESTS=1 pytest -v -m bq  # integration tests against Telemind_Test_DB (need GCP credentials)
```
