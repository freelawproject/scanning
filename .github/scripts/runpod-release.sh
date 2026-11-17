#!/usr/bin/env bash
# Move a RunPod serverless endpoint to a new image (REST API v2).
#
# The endpoint is patched, never its template: in v2 a template is a
# snapshot applied when an endpoint is created, and an edit of it
# reaches no endpoint that exists. The image change is a new release of
# the endpoint, which rolls out as its workers cycle.
#
# Reads RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID and IMAGE from the
# environment.
set -euo pipefail

: "${RUNPOD_API_KEY:?RUNPOD_API_KEY is not set}"
: "${IMAGE:?IMAGE is not set}"
if [ -z "${RUNPOD_ENDPOINT_ID:-}" ]; then
  echo "::error::No endpoint id: store it in this workflow's *_ENDPOINT_ID repository secret"
  exit 1
fi

# Sends only `image`. The PATCH leaves every field it does not name as
# it is, the endpoint's env included.
payload=$(jq -nc --arg img "$IMAGE" '{image: $img}')
response=$(curl -sS -w "\n%{http_code}" -X PATCH \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  --data "$payload" \
  "https://api.runpod.io/v2/serverless/$RUNPOD_ENDPOINT_ID")
body=$(printf '%s\n' "$response" | sed '$d')
status=$(printf '%s\n' "$response" | tail -n1)
echo "HTTP $status"
if [ "$status" -ge 400 ]; then
  echo "$body"
  echo "::error::RunPod endpoint update failed (HTTP $status)"
  exit 1
fi

# The answer is the whole endpoint, env included, so only the fields
# that say what happened are printed.
current=$(printf '%s' "$body" | jq -r '.image // empty')
printf '%s' "$body" | jq '{id, name, image}'
if [ "$current" != "$IMAGE" ]; then
  echo "::error::RunPod answered $status but the endpoint image is '$current', not '$IMAGE'"
  exit 1
fi
