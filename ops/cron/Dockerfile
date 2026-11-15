# Scheduled HTTP jobs (Render crons komputy-cron-*): POST /api/cron/<job> on the web app over the private network.
FROM curlimages/curl:8.11.1
COPY call.sh /call.sh
CMD ["sh", "/call.sh", "events"]
