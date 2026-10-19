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

# Tebra field names — confirm with scripts/discover_fields.py (README, "Campos Tebra").
ENV_VARS="BQ_PROJECT=${PROJECT},BQ_DATASET=Telemind_BD,TEBRA_TIMEZONE=${TEBRA_TIMEZONE:-UTC}"
ENV_VARS+=",TEBRA_CREATED_BY_FIELD=${TEBRA_CREATED_BY_FIELD:-CreatedByUserID}"
ENV_VARS+=",TEBRA_MODIFIED_BY_FIELD=${TEBRA_MODIFIED_BY_FIELD:-LastModifiedByUserID}"
ENV_VARS+=",TEBRA_FROM_MODIFIED_FILTER=${TEBRA_FROM_MODIFIED_FILTER:-FromLastModifiedDate}"
ENV_VARS+=",TEBRA_TO_MODIFIED_FILTER=${TEBRA_TO_MODIFIED_FILTER:-ToLastModifiedDate}"

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
  --task-timeout 21600 --max-retries 1 \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "TEBRA_CUSTOMER_KEY=${SECRET_CUSTOMER_KEY}:latest,TEBRA_USER=${SECRET_USER}:latest,TEBRA_PASSWORD=${SECRET_PASSWORD}:latest"

# Scheduler → run the job every 15 minutes.
gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" --member "serviceAccount:$SA" --role roles/run.invoker >/dev/null
URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "${JOB}-15m" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${JOB}-15m" --location "$REGION" --schedule "*/15 * * * *" --uri "$URI" \
    --http-method POST --oauth-service-account-email "$SA"
else
  gcloud scheduler jobs create http "${JOB}-15m" --location "$REGION" --schedule "*/15 * * * *" --uri "$URI" \
    --http-method POST --oauth-service-account-email "$SA" --paused
  echo "Scheduler created PAUSED. Run the backfill first, then: gcloud scheduler jobs resume ${JOB}-15m --location $REGION"
fi

echo "Done. The job does not read the Google Sheet; share 'Tebra Users' (Viewer) with the people/service accounts that query appointment_prod_authors."
