#!/usr/bin/env bash
# Move the RunPod serverless endpoints of one worker image to a new tag
# (REST API v2).
#
# The endpoint is patched, never its template: in v2 a template is a
# snapshot applied when an endpoint is created, and an edit of it
# reaches no endpoint that exists. The image change is a new release of
# the endpoint, which rolls out as its workers cycle.
#
# The endpoints are found by the repository of the image they run, so
# no endpoint id is stored: every endpoint of the account whose image
# is a tag of IMAGE's repository moves to IMAGE. An endpoint on an
# image of another repository is never touched.
#
# Reads RUNPOD_API_KEY and IMAGE (`repository:tag`) from the
# environment. With ALLOW_NO_ENDPOINT=1 an account with no endpoint on
# the repository is a warning, for an image published before its
# endpoint exists; otherwise it fails the build.
set -euo pipefail

: "${RUNPOD_API_KEY:?RUNPOD_API_KEY is not set}"
: "${IMAGE:?IMAGE is not set}"
API="https://api.runpod.io/v2"
repository="${IMAGE%:*}"

# call METHOD URL [BODY]: sets `body` and `status`, and fails on an
# error answer, whose body holds no endpoint.
call() {
  local response
  response=$(curl -sS -w "\n%{http_code}" -X "$1" \
    -H "Authorization: Bearer $RUNPOD_API_KEY" \
    -H "Content-Type: application/json" \
    ${3:+--data "$3"} \
    "$2")
  body=$(printf '%s\n' "$response" | sed '$d')
  status=$(printf '%s\n' "$response" | tail -n1)
  if [ "$status" -ge 400 ]; then
    echo "$body"
    echo "::error::RunPod answered HTTP $status to $1 ${2%%\?*}"
    exit 1
  fi
}

# Every page of the list. An endpoint's image may carry the registry's
# host, so both spellings of the repository are read.
ids=""
cursor=""
while :; do
  call GET "$API/serverless${cursor:+?cursor=$cursor}"
  page=$(printf '%s' "$body" | jq -r --arg repo "$repository" '
    .endpoints[]
    | select((.image // "") | ltrimstr("docker.io/") | startswith($repo + ":"))
    | .id')
  ids="$ids${page:+$page$'\n'}"
  if [ "$(printf '%s' "$body" | jq -r '.pagination.hasNextPage')" != "true" ]; then
    break
  fi
  cursor=$(printf '%s' "$body" | jq -r '.pagination.nextCursor | @uri')
done

if [ -z "$ids" ]; then
  if [ "${ALLOW_NO_ENDPOINT:-}" = "1" ]; then
    echo "::warning::No RunPod endpoint runs an image of $repository; nothing to update"
    exit 0
  fi
  echo "::error::No RunPod endpoint runs an image of $repository"
  exit 1
fi

# Sends only `image`. The PATCH leaves every field it does not name as
# it is, the endpoint's env included.
payload=$(jq -nc --arg img "$IMAGE" '{image: $img}')
for id in $ids; do
  call PATCH "$API/serverless/$id" "$payload"
  # The answer is the whole endpoint, env included, so only the fields
  # that say what happened are printed.
  printf '%s' "$body" | jq -c '{id, name, image}'
  current=$(printf '%s' "$body" | jq -r '.image // empty')
  if [ "$current" != "$IMAGE" ]; then
    echo "::error::RunPod answered $status but endpoint $id runs '$current', not '$IMAGE'"
    exit 1
  fi
done
