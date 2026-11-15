#!/bin/sh
# Env: DATABASE_URL, STORAGE_ENDPOINT_URL, STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY, BACKUP_BUCKET_NAME.
# Custom-format dump (compressed, restorable per table with pg_restore). Bucket lifecycle deletes after 30 days.
set -eu
key="postgres/$(date -u +%Y/%m/%d)/komputy-$(date -u +%Y%m%dT%H%M%SZ).dump"
export AWS_ACCESS_KEY_ID="$STORAGE_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$STORAGE_SECRET_KEY" AWS_DEFAULT_REGION=auto
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  | aws s3 cp - "s3://$BACKUP_BUCKET_NAME/$key" --endpoint-url "$STORAGE_ENDPOINT_URL" --expected-size 50000000000
echo "backup ok: $key ($(aws s3 ls "s3://$BACKUP_BUCKET_NAME/$key" --endpoint-url "$STORAGE_ENDPOINT_URL" | awk '{print $3}') bytes)"
