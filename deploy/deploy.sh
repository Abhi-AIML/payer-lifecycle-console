#!/usr/bin/env bash
# Tests, builds and deploys the current folder to Cloud Run via Cloud Build (cloudbuild.yaml).
# Run in Cloud Shell from the repository root, after deploy/setup.sh:
#     bash deploy/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-payer-console}"
SHOW_DEMO_PASSWORD="${SHOW_DEMO_PASSWORD:-true}"
[ -n "$PROJECT_ID" ] || { echo "Set a project first: gcloud config set project YOUR_PROJECT_ID"; exit 1; }

gcloud builds submit --config=cloudbuild.yaml \
  --gcs-source-staging-dir="gs://${PROJECT_ID}_cloudbuild/source" \
  --substitutions="_REGION=${REGION},_SERVICE=${SERVICE},_SHOW_DEMO_PASSWORD=${SHOW_DEMO_PASSWORD}"

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
HOST="${URL#https://}"
echo "Deployed: ${URL}"
curl -fsS "${URL}/healthz" && echo "  <- health check"

# A free Cloud Monitoring uptime check on /healthz: alerts if the demo goes down while judges are reviewing,
# and its regular requests keep the single instance warm, so demo state is not lost to scale-to-zero.
if ! gcloud monitoring uptime list-configs --format='value(displayName)' 2>/dev/null | grep -qx "${SERVICE}-healthz"; then
  gcloud monitoring uptime create "${SERVICE}-healthz" --resource-type=uptime-url \
    --resource-labels="host=${HOST},project_id=${PROJECT_ID}" --protocol=https --path=/healthz --period=5 \
    || echo "Could not create the uptime check from the CLI. Create it in the console: Monitoring > Uptime checks > ${URL}/healthz"
fi
