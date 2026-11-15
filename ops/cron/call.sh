#!/bin/sh
# Usage: call.sh <job> [max-seconds]. Env: WEB_HOSTPORT (web service host:port), CRON_SECRET.
set -eu
job="$1"; max="${2:-50}"
code=$(curl -sS -o /tmp/body -w '%{http_code}' -m "$max" -X POST -H "Authorization: Bearer $CRON_SECRET" "http://$WEB_HOSTPORT/api/cron/$job")
echo "cron $job -> HTTP $code $(head -c 500 /tmp/body)"
[ "$code" -ge 200 ] && [ "$code" -lt 300 ]
