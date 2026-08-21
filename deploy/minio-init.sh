#!/bin/sh
set -eu

: "${STORAGE_ACCESS_KEY:?STORAGE_ACCESS_KEY is required}"
: "${STORAGE_SECRET_KEY:?STORAGE_SECRET_KEY is required}"
: "${STORAGE_BUCKET:?STORAGE_BUCKET is required}"

alias_name="production"
bucket="${alias_name}/${STORAGE_BUCKET}"

mc alias set "$alias_name" http://storage:9000 "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY"
mc mb --ignore-existing "$bucket"

# User-uploaded media is private. Django returns time-limited SigV4 URLs when a client is allowed
# to see an object; MinIO itself must never grant anonymous list/read/write access.
mc anonymous set none "$bucket"

# Fail initialization if either the bucket or its privacy policy cannot be read back.
mc stat "$bucket" >/dev/null
policy="$(mc anonymous get "$bucket")"
case "$policy" in
  *none*|*private*) ;;
  *)
    echo "Expected anonymous policy 'none' for ${STORAGE_BUCKET}; got: ${policy}" >&2
    exit 1
    ;;
esac

echo "MinIO bucket ${STORAGE_BUCKET} exists and anonymous access is disabled."
