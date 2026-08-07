#!/usr/bin/env bash
# Deploy "Should I Drive to Sheepshead Bay?" to Google Cloud Run.
#
#   ./deploy.sh                 # deploy with defaults
#   REGION=us-east4 ./deploy.sh # override region
#
# Prereqs (one time):
#   gcloud auth login
#   gcloud config set project YOUR_PROJECT_ID
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

set -euo pipefail

SERVICE="${SERVICE:-sheepshead-bay}"
REGION="${REGION:-us-east1}"

echo "Deploying '${SERVICE}' to Cloud Run in ${REGION}..."

# A Dockerfile is present at the repo root, so --source . builds with it
# via Cloud Build (buildpacks are only used when there is no Dockerfile).
gcloud run deploy "${SERVICE}" \
  --source . \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --timeout 60 \
  --max-instances 5

echo
echo "Done. Service URL:"
gcloud run services describe "${SERVICE}" \
  --region "${REGION}" \
  --format 'value(status.url)'
