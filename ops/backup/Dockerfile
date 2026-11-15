# Nightly off-site backup (Render cron `komputy-cron-backup`): pg_dump → Cloudflare R2. Restore: see plans/RUNBOOK.md.
FROM postgres:17-alpine
RUN apk add --no-cache aws-cli
COPY backup.sh /backup.sh
USER postgres
CMD ["sh", "/backup.sh"]
