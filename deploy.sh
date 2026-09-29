#!/usr/bin/env bash
# deploy.sh — Build, push, and deploy Flipkart PO Automation to Cloud Run Jobs
#
# One-time setup: fill in and run create_secrets.ps1, which creates each secret listed in
# SECRET_BINDINGS and gives the job's service account roles/secretmanager.secretAccessor.
#
# Usage:
#   ./deploy.sh
#   NOTIFICATION_EMAIL="a@x.com,b@y.com" ./deploy.sh    # override alert recipients
#
# Schedule: Cloud Scheduler job flipkart-po-automation-daily runs it daily at 11:00 and 17:00 IST.
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-seoai-479305}"
REGION="${GCP_REGION:-asia-south1}"
JOB_NAME="flipkart-po-automation"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/holistique/${JOB_NAME}"
TAG="${IMAGE}:latest"
SA="254146383960-compute@developer.gserviceaccount.com"
TIMEOUT=1800   # login + a handful of POs takes a few minutes; leaves room for a large backlog

SECRET_BINDINGS=(
  VENDORHUB_USERNAME=flipkart-po-vendorhub-username
  VENDORHUB_PASSWORD=flipkart-po-vendorhub-password
  SESSION_ENC_KEY=flipkart-po-session-enc-key
  CAPSOLVER_API_KEY=flipkart-po-capsolver-api-key
  DB_HOST=flipkart-po-db-host
  DB_USER=flipkart-po-db-user
  DB_PASSWORD=flipkart-po-db-password
  TENANT_ID=flipkart-po-ms-tenant-id
  CLIENT_ID=flipkart-po-ms-client-id
  CLIENT_SECRET=flipkart-po-ms-client-secret
)

SECRET_ARGS=()
for binding in "${SECRET_BINDINGS[@]}"; do
  SECRET_ARGS+=("${binding}:latest")
done
SECRET_SPEC="$(IFS=,; echo "${SECRET_ARGS[*]}")"

# "^|^" makes | the separator, so NOTIFICATION_EMAIL can hold a comma-separated list
ENV_VARS="^|^VENDORHUB_BASE_URL=https://vendorhub.flipkart.com"
ENV_VARS+="|DB_NAME=Holistique"
ENV_VARS+="|SCOPE=https://graph.microsoft.com/.default"
ENV_VARS+="|MAILBOX_USER=${MAILBOX_USER:-AI@holistique.in}"
ENV_VARS+="|NOTIFICATION_EMAIL=${NOTIFICATION_EMAIL:-siddhant.pardhe@glidebrands.in,Shahana@glidebrands.in,Rahul@glidebrands.in,Akash.Jaiswar@glidebrands.in}"
ENV_VARS+="|HEADLESS=true"
ENV_VARS+="|LOG_LEVEL=INFO"
ENV_VARS+="|TZ=Asia/Kolkata"

echo "==> Building and pushing image via Cloud Build: ${TAG}"
gcloud builds submit --tag "${TAG}" . --project="${PROJECT}"

COMMON_JOB_ARGS=(
  "--image=${TAG}"
  "--region=${REGION}"
  "--project=${PROJECT}"
  "--service-account=${SA}"
  "--task-timeout=${TIMEOUT}"
  "--max-retries=0"   # a retry would re-run the PO acknowledgement step and send a second email
  "--parallelism=1"
  "--cpu=1"
  "--memory=1Gi"      # headless Chromium for the whole run
  "--set-secrets=${SECRET_SPEC}"
  "--set-env-vars=${ENV_VARS}"
)

echo "==> Deploying Cloud Run Job: ${JOB_NAME} (region: ${REGION})"
if gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --project="${PROJECT}" &>/dev/null; then
  gcloud run jobs update "${JOB_NAME}" "${COMMON_JOB_ARGS[@]}"
else
  echo "==> Job not found — creating..."
  gcloud run jobs create "${JOB_NAME}" "${COMMON_JOB_ARGS[@]}"
fi

echo ""
echo "==> Deployment complete."
echo ""
echo "Manual trigger:"
echo "  gcloud run jobs execute ${JOB_NAME} --region=${REGION} --project=${PROJECT} --wait"
echo ""
echo "Scheduler (already created as ${JOB_NAME}-daily: daily 11:00 and 17:00 IST). To recreate:"
echo "  gcloud scheduler jobs create http ${JOB_NAME}-daily \\"
echo "    --location=${REGION} --project=${PROJECT} \\"
echo "    --schedule='0 11,17 * * *' --time-zone='Asia/Kolkata' \\"
echo "    --http-method=POST \\"
echo "    --uri='https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB_NAME}:run' \\"
echo "    --oauth-service-account-email=${SA}"
