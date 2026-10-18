#!/usr/bin/env bash
# One-time Google Cloud setup for the Payer Lifecycle Console.
# Run in Cloud Shell (gcloud is preinstalled) from the repository root:
#     gcloud config set project YOUR_PROJECT_ID
#     bash deploy/setup.sh
# Safe to re-run: each step skips what already exists. Existing secrets are never overwritten.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-payer}"
[ -n "$PROJECT_ID" ] || { echo "Set a project first: gcloud config set project YOUR_PROJECT_ID"; exit 1; }
RUN_SA="payer-console-run@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="payer-console-build@${PROJECT_ID}.iam.gserviceaccount.com"
STAGING="gs://${PROJECT_ID}_cloudbuild"
echo "Project: ${PROJECT_ID}   Region: ${REGION}"

echo "[1/6] Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iam.googleapis.com logging.googleapis.com monitoring.googleapis.com storage.googleapis.com

echo "[2/6] Artifact Registry repository, with a cleanup policy that keeps storage inside the free 0.5 GB"
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" --description="Payer Lifecycle Console images"
gcloud artifacts repositories set-cleanup-policies "$REPO" --location="$REGION" --policy=deploy/ar-cleanup-policy.json --no-dry-run

echo "[3/6] Service accounts: one to run the app, one to build and deploy it"
create_sa() {
  gcloud iam service-accounts describe "$1@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$1" --display-name="$2"
}
create_sa payer-console-run "Payer console runtime (reads its secrets, nothing else)"
create_sa payer-console-build "Payer console build and deploy"

echo "[4/6] Secrets in Secret Manager (readable only by the runtime service account)"
new_secret() {  # name, value: created once, never overwritten on re-runs
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    echo "  $1 exists, kept. To rotate: printf 'NEW' | gcloud secrets versions add $1 --data-file=-"
  else
    gcloud secrets create "$1" --replication-policy=automatic
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=-
  fi
  gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:${RUN_SA}" --role=roles/secretmanager.secretAccessor >/dev/null
}
if ! gcloud secrets describe gemini-api-key >/dev/null 2>&1; then
  read -r -s -p "  Gemini API key from aistudio.google.com (input hidden): " GEMINI_KEY; echo
  [ -n "$GEMINI_KEY" ] || { echo "  A Gemini key is required. Create one at https://aistudio.google.com/apikey"; exit 1; }
  read -r -p "  Demo password for the judges (Enter keeps the published default MaatriDemo#1): " DEMO_PW
fi
new_secret gemini-api-key "${GEMINI_KEY:-}"
new_secret sandbox-token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
new_secret demo-password "${DEMO_PW:-MaatriDemo#1}"

echo "[5/6] Least-privilege roles for the build service account"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${BUILD_SA}" --role=roles/run.admin --condition=None >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${BUILD_SA}" --role=roles/logging.logWriter --condition=None >/dev/null
gcloud artifacts repositories add-iam-policy-binding "$REPO" --location="$REGION" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/artifactregistry.writer >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/iam.serviceAccountUser >/dev/null

echo "[6/6] Source staging bucket for Cloud Build (free-tier region), readable by the build account"
gcloud storage buckets describe "$STAGING" >/dev/null 2>&1 \
  || gcloud storage buckets create "$STAGING" --location="$REGION" --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "$STAGING" --member="serviceAccount:${BUILD_SA}" --role=roles/storage.objectViewer >/dev/null

echo "Setup done. Deploy with: bash deploy/deploy.sh"
