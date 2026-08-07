#!/usr/bin/env bash
# Deploy to Cloud Run.
#
#     ./deploy.sh
#
# Secrets live in .env.yaml (gitignored, dockerignored) and are passed as runtime
# environment variables. They never enter the image. Keeping them in a file rather
# than on the command line also means this command is short enough that a terminal
# line-wrap can't split it into two broken commands.

set -euo pipefail

SERVICE="${SERVICE:-sheepshead}"
REGION="${REGION:-us-east1}"
ENV_FILE="${ENV_FILE:-.env.yaml}"

cd "$(dirname "$0")"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE. Create it with:" >&2
  echo '  GOOGLE_API_KEY: "your-key"' >&2
  echo '  DETECTOR: "gemini"' >&2
  exit 1
fi

echo "Deploying $SERVICE to $REGION ..."

# --no-cpu-throttling and --min-instances 1 are both load-bearing, not tuning.
#
# By default Cloud Run allocates CPU only while a request is in flight, which
# freezes the background refresh task between requests -- the cache then just ages
# and never updates. --no-cpu-throttling keeps the loop alive.
#
# --min-instances 1 keeps one container warm. Without it the service scales to zero
# and the next visitor pays a ~10s cold start, which is not a thing you want
# happening while you are standing in front of judges.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --timeout 120 \
  --min-instances 1 \
  --no-cpu-throttling \
  --env-vars-file "$ENV_FILE"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "Live: $URL"
echo "Checking it actually serves..."
curl -sS -m 30 -o /dev/null -w "  /healthz     HTTP %{http_code}\n" "$URL/healthz"
curl -sS -m 90 -o /dev/null -w "  /api/verdict HTTP %{http_code}  %{time_total}s\n" "$URL/api/verdict"
echo
echo "$URL"
