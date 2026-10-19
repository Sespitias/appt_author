#!/usr/bin/env bash
# Build and deploy the Cloud Run Job + a Cloud Scheduler trigger every 15 minutes.
# Prereqs: gcloud authenticated on the project; Secret Manager already holds the Tebra
# credentials used by the existing appointment_prod service (set the names below).
set -euo pipefail

PROJECT="${PROJECT:-weekly-revenue-integration}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-tebra-appt-authors}"
REPO="${REPO:-cloud-run}"                       # Artifact Registry repo
SA_NAME="${SA_NAME:-tebra-appt-authors}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:$(date +%Y%m%d%H%M%S)"

# Secret names in Secret Manager (reuse the existing service's secrets).
SECRET_CUSTOMER_KEY="${SECRET_CUSTOMER_KEY:-TEBRA_CUSTOMER_KEY}"
SECRET_USER="${SECRET_USER:-TEBRA_USER}"
SECRET_PASSWORD="${SECRET_PASSWORD:-TEBRA_PASSWORD}"

# Tuning (see README): Tebra allows one GetAppointment call per 250 ms.
ENV_VARS="BQ_PROJECT=${PROJECT},BQ_DATASET=Telemind_BD"
ENV_VARS+=",TEBRA_MIN_INTERVAL=${TEBRA_MIN_INTERVAL:-0.30},MAX_RUN_SECONDS=${MAX_RUN_SECONDS:-720},BATCH_SIZE=${BATCH_SIZE:-500}"

gcloud config set project "$PROJECT" >/dev/null

# Service account + permissions (idempotent).
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name "Tebra appointment authors job"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/bigquery.jobUser --condition=None >/dev/null
bq add-iam-policy-binding --member="serviceAccount:$SA" --role=roles/bigquery.dataEditor "${PROJECT}:Telemind_BD" >/dev/null
for s in "$SECRET_CUSTOMER_KEY" "$SECRET_USER" "$SECRET_PASSWORD"; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null
done

gcloud builds submit --tag "$IMAGE" .

gcloud run jobs deploy "$JOB" \
  --image "$IMAGE" --region "$REGION" --service-account "$SA" \
  --task-timeout 840 --max-retries 0 --parallelism 1 --tasks 1 \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "TEBRA_CUSTOMER_KEY=${SECRET_CUSTOMER_KEY}:latest,TEBRA_USER=${SECRET_USER}:latest,TEBRA_PASSWORD=${SECRET_PASSWORD}:latest"

# Scheduler → run the job every 15 minutes.
gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" --member "serviceAccount:$SA" --role roles/run.invoker >/dev/null
URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "${JOB}-15m" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${JOB}-15m" --location "$REGION" --schedule "*/15 * * * *" --uri "$URI" \
    --http-method POST --oauth-service-account-email "$SA"
else
  # Runs every 15 min. Scheduler does not wait for the previous execution: what prevents overlap is
  # MAX_RUN_SECONDS (720 s, incl. a 60 s flush reserve) plus the 840 s task timeout, both under 15 min.
  # The historical backlog drains automatically, newest appointments first.
  gcloud scheduler jobs create http "${JOB}-15m" --location "$REGION" --schedule "*/15 * * * *" --uri "$URI" \
    --http-method POST --oauth-service-account-email "$SA"
fi

echo "Done. The job does not read the Google Sheet; share 'Tebra Users' (Viewer) with the people/service accounts that query appointment_prod_authors."
